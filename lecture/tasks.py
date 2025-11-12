from celery import shared_task
from celery.signals import task_failure, task_postrun, worker_ready
from .models import Lecture, PdfChunk, Mapping, ProcessingStats
from .services import (
    init_gemini_models, init_chromadb_client, init_ollama_client,
    process_audio, process_pdf, get_pdf_page_count, get_summary_from_gemini,
    embed_and_store, create_semantic_mappings
)
import time
import subprocess
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from django.db import transaction
from django.utils import timezone
from django.conf import settings
from datetime import timedelta

def get_audio_duration_fast(audio_path):
    """
    빠르게 오디오 길이를 측정합니다.
    먼저 mutagen을 시도하고, 실패하면 ffprobe를 사용합니다.
    """
    # 방법 1: mutagen 사용 (가장 빠름, 메타데이터만 읽음)
    try:
        from mutagen import File
        audio_file = File(audio_path)
        if audio_file is not None and hasattr(audio_file, 'info'):
            duration = audio_file.info.length
            if duration and duration > 0:
                return duration
    except (ImportError, AttributeError, Exception):
        pass
    
    # 방법 2: ffprobe 사용 (빠름)
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', audio_path],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            duration = float(result.stdout.strip())
            if duration and duration > 0:
                return duration
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError, Exception):
        pass
    
    # 방법 3: pydub 사용 (느림, 마지막 수단)
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_file(audio_path)
        return len(audio) / 1000.0
    except (ImportError, Exception):
        pass
    
    return None

# --- 자식 작업 함수들 (병렬 실행용) ---

def _stt_worker(audio_path, model_flash):
    """
    STT 처리 작업 함수
    병렬 그룹 1에서 실행됩니다.
    """
    try:
        print(f"[STT Worker] 시작...")
        stt_start_time = time.time()
        
        full_script_ts, script_text_only = process_audio(audio_path, model_flash)
        if full_script_ts is None or script_text_only is None:
            raise Exception("STT 처리 실패: 오디오 파일을 텍스트로 변환할 수 없습니다.")
        
        stt_elapsed_sec = time.time() - stt_start_time
        print(f"[STT Worker] 완료 (소요 시간: {stt_elapsed_sec:.2f}초)")
        
        return {
            'success': True,
            'full_script_ts': full_script_ts,
            'script_text_only': script_text_only,
            'elapsed_sec': stt_elapsed_sec
        }
    except Exception as e:
        print(f"[STT Worker] 실패: {e}")
        return {
            'success': False,
            'error': str(e)
        }

def _pdf_worker(pdf_path, ollama_client):
    """
    PDF 파싱 작업 함수
    병렬 그룹 1에서 실행됩니다.
    """
    try:
        print(f"[PDF Worker] 시작...")
        pdf_parse_start_time = time.time()
        
        pdf_texts = process_pdf(pdf_path, ollama_client=ollama_client)
        if not pdf_texts:
            raise Exception("PDF 파싱 실패: PDF 파일을 읽을 수 없습니다.")
        
        pdf_parse_elapsed_sec = time.time() - pdf_parse_start_time
        pdf_page_count = len(pdf_texts) if pdf_texts else 0
        print(f"[PDF Worker] 완료 (소요 시간: {pdf_parse_elapsed_sec:.2f}초, 페이지 수: {pdf_page_count})")
        
        return {
            'success': True,
            'pdf_texts': pdf_texts,
            'page_count': pdf_page_count,
            'elapsed_sec': pdf_parse_elapsed_sec
        }
    except Exception as e:
        print(f"[PDF Worker] 실패: {e}")
        return {
            'success': False,
            'error': str(e)
        }

def _summary_worker(full_script_ts, model_flash):
    """
    스크립트 요약 작업 함수
    병렬 그룹 2에서 실행됩니다.
    STT 결과(full_script_ts)가 필요합니다.
    """
    try:
        print(f"[Summary Worker] 시작...")
        summary_start_time = time.time()
        
        summary_json = get_summary_from_gemini(model_flash, full_script_ts)
        if summary_json is None:
            raise Exception("요약 생성 실패: 스크립트 요약을 생성할 수 없습니다.")
        
        summary_elapsed_sec = time.time() - summary_start_time
        print(f"[Summary Worker] 완료 (소요 시간: {summary_elapsed_sec:.2f}초)")
        
        return {
            'success': True,
            'summary_json': summary_json,
            'elapsed_sec': summary_elapsed_sec
        }
    except Exception as e:
        print(f"[Summary Worker] 실패: {e}")
        return {
            'success': False,
            'error': str(e)
        }

def _embedding_worker(lecture_id, pdf_texts, full_script_ts, model_embedding, chroma_client):
    """
    임베딩 작업 함수
    병렬 그룹 2에서 실행됩니다.
    STT 결과(full_script_ts)와 PDF 결과(pdf_texts)가 필요합니다.
    """
    try:
        print(f"[Embedding Worker] 시작...")
        embed_start_time = time.time()
        
        embed_and_store(lecture_id, pdf_texts, full_script_ts, model_embedding, chroma_client)
        
        embed_elapsed_sec = time.time() - embed_start_time
        print(f"[Embedding Worker] 완료 (소요 시간: {embed_elapsed_sec:.2f}초)")
        
        return {
            'success': True,
            'elapsed_sec': embed_elapsed_sec
        }
    except Exception as e:
        print(f"[Embedding Worker] 실패: {e}")
        return {
            'success': False,
            'error': str(e)
        }

def mark_lecture_as_failed(lecture_id, error_message=None):
    """
    강의를 실패 상태로 표시하는 헬퍼 함수
    """
    try:
        with transaction.atomic():
            lecture = Lecture.objects.select_for_update().get(id=lecture_id)
            if lecture.status == 'processing':  # 아직 처리 중인 경우에만 실패로 표시
                lecture.status = 'failed'
                lecture.save()
                print(f"강의 {lecture_id}를 실패 상태로 표시했습니다. (오류: {error_message})")
    except Lecture.DoesNotExist:
        print(f"강의 {lecture_id}를 찾을 수 없습니다.")
    except Exception as e:
        print(f"강의 {lecture_id} 상태 업데이트 실패: {e}")

def check_and_mark_stuck_tasks(minutes=18, dry_run=False):
    """
    오래된 '처리 중' 상태의 강의를 감지하고 실패로 표시하는 함수
    
    Args:
        minutes: 몇 분 이상 지난 작업을 실패로 표시할지 지정 (기본값: 18)
        dry_run: 실제로 상태를 변경하지 않고 확인만 할지 여부 (기본값: False)
    
    Returns:
        tuple: (발견된 작업 수, 업데이트된 작업 수)
    """
    cutoff_time = timezone.now() - timedelta(minutes=minutes)
    
    # '처리 중' 상태이고 지정된 시간 이상 지난 강의 찾기
    stuck_lectures = Lecture.objects.filter(
        status='processing',
        created_at__lt=cutoff_time
    ).order_by('created_at')
    
    count = stuck_lectures.count()
    updated_count = 0
    
    if count > 0:
        print(f"[오래된 작업 감지] {count}개의 오래된 '처리 중' 상태 강의를 발견했습니다. (기준: {minutes}분 이상)")
        
        for lecture in stuck_lectures:
            age_minutes = (timezone.now() - lecture.created_at).total_seconds() / 60
            print(f"  - 강의 ID {lecture.id}: '{lecture.lecture_name}' (경과: {age_minutes:.1f}분)")
            
            if not dry_run:
                lecture.status = 'failed'
                lecture.save()
                updated_count += 1
        
        if not dry_run:
            print(f"[오래된 작업 감지] {updated_count}개의 강의 상태를 '실패'로 업데이트했습니다.")
        else:
            print(f"[오래된 작업 감지] --dry-run 모드: {count}개의 강의가 업데이트될 것입니다.")
    else:
        print(f"[오래된 작업 감지] 오래된 '처리 중' 상태의 강의가 없습니다. (기준: {minutes}분 이상)")
    
    return count, updated_count

@shared_task(bind=True, max_retries=0)
def process_lecture_task(self, lecture_id):
    """
    강의 처리 메인 태스크 (병렬 처리 버전)
    
    병렬 그룹 1: STT + PDF 파싱 (동시 실행)
    병렬 그룹 2: 요약 + 임베딩 (동시 실행, 그룹 1 완료 후)
    순차 처리: 매핑 + 데이터 저장
    
    Args:
        self: Celery 작업 인스턴스 (bind=True로 인해 자동 전달)
        lecture_id: 처리할 강의 ID
    """
    try:
        lecture = Lecture.objects.get(id=lecture_id)
        models = init_gemini_models()
        chroma_client = init_chromadb_client()
        ollama_client = init_ollama_client()

        start_time = time.time()
        
        # 오디오 파일 확인
        if not lecture.audio_file:
            raise Exception("오디오 파일이 없습니다. YouTube 다운로드가 완료되지 않았을 수 있습니다.")
        
        # 오디오 길이와 PDF 페이지 수 계산 (통계 업데이트용)
        audio_path = lecture.audio_file.path
        pdf_path = lecture.pdf_file.path
        
        try:
            audio_duration_sec = get_audio_duration_fast(audio_path)
            audio_duration_min = audio_duration_sec / 60.0 if audio_duration_sec else 0
        except Exception as e:
            print(f"오디오 길이 계산 실패: {e}")
            audio_duration_min = 0
        
        # 단계별 소요 시간 저장용 딕셔너리
        step_times = {}
        
        # ============================================
        # 병렬 그룹 1: STT + PDF 파싱 (동시 실행)
        # ============================================
        print("=" * 20)
        print("병렬 그룹 1 시작: STT + PDF 파싱 (동시 실행)")
        print("=" * 20)
        Lecture.objects.filter(id=lecture_id).update(current_step=1)
        
        group1_start_time = time.time()
        stt_result = None
        pdf_result = None
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            # STT와 PDF 파싱을 동시에 실행
            stt_future = executor.submit(_stt_worker, audio_path, models['flash'])
            pdf_future = executor.submit(_pdf_worker, pdf_path, ollama_client)
            
            # 두 작업이 모두 완료될 때까지 대기
            for future in as_completed([stt_future, pdf_future]):
                try:
                    result = future.result()
                    if 'full_script_ts' in result:
                        stt_result = result
                        step_times['1'] = result['elapsed_sec']
                        Lecture.objects.filter(id=lecture_id).update(step_times=step_times)
                        print(f"[병렬 그룹 1] STT 완료 (소요 시간: {result['elapsed_sec']:.2f}초)")
                    elif 'pdf_texts' in result:
                        pdf_result = result
                        step_times['2'] = result['elapsed_sec']
                        Lecture.objects.filter(id=lecture_id).update(step_times=step_times)
                        print(f"[병렬 그룹 1] PDF 파싱 완료 (소요 시간: {result['elapsed_sec']:.2f}초)")
                except Exception as e:
                    print(f"[병렬 그룹 1] 작업 실패: {e}")
                    raise
        
        group1_elapsed_sec = time.time() - group1_start_time
        print(f"[병렬 그룹 1] 전체 완료 (소요 시간: {group1_elapsed_sec:.2f}초)")
        
        # 결과 검증
        if not stt_result or not stt_result.get('success'):
            error_msg = stt_result.get('error', '알 수 없는 오류') if stt_result else 'STT 결과 없음'
            raise Exception(f"STT 처리 실패: {error_msg}")
        
        if not pdf_result or not pdf_result.get('success'):
            error_msg = pdf_result.get('error', '알 수 없는 오류') if pdf_result else 'PDF 결과 없음'
            raise Exception(f"PDF 파싱 실패: {error_msg}")
        
        # 결과 추출
        full_script_ts = stt_result['full_script_ts']
        script_text_only = stt_result['script_text_only']
        stt_elapsed_sec = stt_result['elapsed_sec']
        
        pdf_texts = pdf_result['pdf_texts']
        pdf_page_count = pdf_result['page_count']
        pdf_parse_elapsed_sec = pdf_result['elapsed_sec']
        
        # ============================================
        # 병렬 그룹 2: 요약 + 임베딩 (동시 실행)
        # ============================================
        print("=" * 20)
        print("병렬 그룹 2 시작: 요약 + 임베딩 (동시 실행)")
        print("=" * 20)
        Lecture.objects.filter(id=lecture_id).update(current_step=3)
        
        group2_start_time = time.time()
        summary_result = None
        embedding_result = None
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            # 요약과 임베딩을 동시에 실행
            summary_future = executor.submit(_summary_worker, full_script_ts, models['flash'])
            embedding_future = executor.submit(_embedding_worker, lecture.id, pdf_texts, full_script_ts, models['embedding'], chroma_client)
            
            # 두 작업이 모두 완료될 때까지 대기
            for future in as_completed([summary_future, embedding_future]):
                try:
                    result = future.result()
                    # 요약 결과인지 임베딩 결과인지 구분
                    if 'summary_json' in result:
                        summary_result = result
                        step_times['3'] = result['elapsed_sec']
                        Lecture.objects.filter(id=lecture_id).update(step_times=step_times)
                        print(f"[병렬 그룹 2] 요약 완료 (소요 시간: {result['elapsed_sec']:.2f}초)")
                    else:
                        # 임베딩 결과 (summary_json이 없고 success와 elapsed_sec만 있음)
                        embedding_result = result
                        step_times['4'] = result['elapsed_sec']
                        Lecture.objects.filter(id=lecture_id).update(step_times=step_times)
                        print(f"[병렬 그룹 2] 임베딩 완료 (소요 시간: {result['elapsed_sec']:.2f}초)")
                except Exception as e:
                    print(f"[병렬 그룹 2] 작업 실패: {e}")
                    raise
        
        group2_elapsed_sec = time.time() - group2_start_time
        print(f"[병렬 그룹 2] 전체 완료 (소요 시간: {group2_elapsed_sec:.2f}초)")
        
        # 결과 검증
        if not summary_result or not summary_result.get('success'):
            error_msg = summary_result.get('error', '알 수 없는 오류') if summary_result else '요약 결과 없음'
            raise Exception(f"요약 생성 실패: {error_msg}")
        
        if not embedding_result or not embedding_result.get('success'):
            error_msg = embedding_result.get('error', '알 수 없는 오류') if embedding_result else '임베딩 결과 없음'
            raise Exception(f"임베딩 실패: {error_msg}")
        
        # 결과 추출
        summary_json = summary_result['summary_json']
        summary_elapsed_sec = summary_result['elapsed_sec']
        embed_elapsed_sec = embedding_result['elapsed_sec']
        
        # ============================================
        # 순차 처리: 매핑 + 데이터 저장
        # ============================================
        print("=" * 20)
        print("순차 처리 시작: 매핑 + 데이터 저장")
        print("=" * 20)
        
        # 5. 매핑
        print("5/6: 의미 기반 매핑 시작...")
        Lecture.objects.filter(id=lecture_id).update(current_step=5)
        mapping_start_time = time.time()
        
        mappings_to_create = create_semantic_mappings(lecture.id, summary_json, models['embedding'], chroma_client)
        
        mapping_elapsed_sec = time.time() - mapping_start_time
        step_times['5'] = mapping_elapsed_sec
        Lecture.objects.filter(id=lecture_id).update(step_times=step_times)
        print(f"5/6: 매핑 완료 (소요 시간: {mapping_elapsed_sec:.2f}초)")
        
        # PDF 처리 시간 = 파싱 + 임베딩 + 매핑
        pdf_processing_elapsed_sec = pdf_parse_elapsed_sec + embed_elapsed_sec + mapping_elapsed_sec
        
        # 6. 데이터 저장
        print("6/6: 데이터 저장 시작...")
        Lecture.objects.filter(id=lecture_id).update(current_step=6)
        save_start_time = time.time()
        
        lecture = Lecture.objects.get(id=lecture_id)
        lecture.full_script = full_script_ts
        lecture.summary_json = summary_json
        lecture.status = 'completed' # 상태를 '완료'로 변경
        lecture.save()

        # PdfChunk 및 Mapping 모델에도 저장 
        for page_num, content in pdf_texts:
            PdfChunk.objects.create(lecture=lecture, page_num=page_num, content=content)
        
        # Mapping 정보 대량 저장 (bulk_create)
        Mapping.objects.bulk_create(
            [Mapping(lecture=lecture, **m) for m in mappings_to_create]
        )
        
        save_elapsed_sec = time.time() - save_start_time
        step_times['6'] = save_elapsed_sec
        lecture.step_times = step_times
        lecture.save()
        print(f"6/6: 데이터 저장 완료 (소요 시간: {save_elapsed_sec:.2f}초)")

        total_elapsed_sec = time.time() - start_time
        print("=" * 20)
        print(f"처리 완료 (총 {total_elapsed_sec:.2f}초)")
        print(f"  - 병렬 그룹 1 (STT+PDF): {group1_elapsed_sec:.2f}초")
        print(f"  - 병렬 그룹 2 (요약+임베딩): {group2_elapsed_sec:.2f}초")
        print(f"  - 순차 처리 (매핑+저장): {mapping_elapsed_sec + save_elapsed_sec:.2f}초")
        print("=" * 20)
        
        # 7. ProcessingStats 업데이트 (이동 평균 방식)
        try:
            stats = ProcessingStats.get_or_create_singleton()
            
            # STT 평균 업데이트 (1분당 초)
            if audio_duration_min > 0:
                stt_sec_per_min = stt_elapsed_sec / audio_duration_min
                # 이동 평균: 기존 평균과 새 값의 가중 평균 (기존 50%, 새 50%)
                stats.audio_stt_avg_sec_per_min = stats.audio_stt_avg_sec_per_min * 0.5 + stt_sec_per_min * 0.5
            
            # PDF 파싱 평균 업데이트 (1페이지당 초)
            if pdf_page_count > 0:
                pdf_parsing_sec_per_page = pdf_parse_elapsed_sec / pdf_page_count
                # 이동 평균: 기존 평균과 새 값의 가중 평균 (기존 50%, 새 50%)
                stats.pdf_parsing_avg_sec_per_page = stats.pdf_parsing_avg_sec_per_page * 0.5 + pdf_parsing_sec_per_page * 0.5
            
            # 임베딩 평균 업데이트 (1페이지당 초)
            if pdf_page_count > 0:
                embedding_sec_per_page = embed_elapsed_sec / pdf_page_count
                # 이동 평균: 기존 평균과 새 값의 가중 평균 (기존 50%, 새 50%)
                stats.embedding_avg_sec_per_page = stats.embedding_avg_sec_per_page * 0.5 + embedding_sec_per_page * 0.5
            
            # PDF 처리 평균 업데이트 (하위 호환성 유지: 파싱+임베딩+매핑)
            if pdf_page_count > 0:
                pdf_sec_per_page = pdf_processing_elapsed_sec / pdf_page_count
                # 이동 평균: 기존 평균과 새 값의 가중 평균 (기존 50%, 새 50%)
                stats.pdf_processing_avg_sec_per_page = stats.pdf_processing_avg_sec_per_page * 0.5 + pdf_sec_per_page * 0.5
            
            # 요약 평균 업데이트 (고정값)
            # 이동 평균: 기존 평균과 새 값의 가중 평균 (기존 50%, 새 50%)
            stats.summary_avg_sec = stats.summary_avg_sec * 0.5 + summary_elapsed_sec * 0.5
            
            stats.save()
            print(f"ProcessingStats 업데이트 완료:")
            print(f"  - STT: {stats.audio_stt_avg_sec_per_min:.2f}초/분")
            print(f"  - PDF 파싱: {stats.pdf_parsing_avg_sec_per_page:.2f}초/페이지")
            print(f"  - 임베딩: {stats.embedding_avg_sec_per_page:.2f}초/페이지")
            print(f"  - 요약: {stats.summary_avg_sec:.2f}초")
        except Exception as e:
            print(f"ProcessingStats 업데이트 실패: {e}")

    except Exception as e:
        print(f"작업 실패: {e}")
        error_message = str(e)
        # 실패 상태로 표시
        mark_lecture_as_failed(lecture_id, error_message)
        # 예외를 다시 발생시켜 Celery가 실패를 기록하도록 함
        raise

@shared_task
def calculate_etr_task(lecture_id):
    """
    ETR(예상 소요 시간)을 빠르게 계산하는 별도 태스크
    업로드 시 즉시 리다이렉트하기 위해 비동기로 실행됩니다.
    """
    try:
        lecture = Lecture.objects.get(id=lecture_id)
        
        # 오디오 길이 계산 (빠른 방법 사용)
        audio_duration_min = 0
        if lecture.audio_file:
            try:
                audio_path = lecture.audio_file.path
                audio_duration_sec = get_audio_duration_fast(audio_path)
                audio_duration_min = audio_duration_sec / 60.0 if audio_duration_sec else 0
            except Exception as e:
                print(f"ETR 계산: 오디오 길이 계산 실패: {e}")
                audio_duration_min = 0
        else:
            # YouTube 다운로드가 아직 완료되지 않은 경우
            print(f"ETR 계산: 오디오 파일이 아직 준비되지 않았습니다. (YouTube 다운로드 중일 수 있음)")
            audio_duration_min = 0
        
        # PDF 페이지 수 계산 (빠른 계산용, Ollama 사용 안 함)
        try:
            pdf_path = lecture.pdf_file.path
            pdf_page_count = get_pdf_page_count(pdf_path)
        except Exception as e:
            print(f"ETR 계산: PDF 페이지 수 계산 실패: {e}")
            pdf_page_count = 0
        
        # ProcessingStats에서 평균값 가져오기
        stats = ProcessingStats.get_or_create_singleton()
        
        # ETR 계산 (병렬 처리 구조에 맞게)
        # 병렬 그룹 1: STT와 PDF 파싱 중 긴 시간
        stt_estimated_sec = audio_duration_min * stats.audio_stt_avg_sec_per_min
        pdf_parsing_estimated_sec = pdf_page_count * stats.pdf_parsing_avg_sec_per_page
        group1_estimated_sec = max(stt_estimated_sec, pdf_parsing_estimated_sec)
        
        # 병렬 그룹 2: 요약과 임베딩 중 긴 시간
        summary_estimated_sec = stats.summary_avg_sec
        embedding_estimated_sec = pdf_page_count * stats.embedding_avg_sec_per_page
        group2_estimated_sec = max(summary_estimated_sec, embedding_estimated_sec)
        
        # 순차 처리: 매핑 시간 (PDF 처리 평균에서 파싱과 임베딩을 제외한 나머지로 추정)
        # 매핑 시간 = (PDF 처리 평균 - PDF 파싱 평균 - 임베딩 평균) * 페이지 수
        mapping_avg_sec_per_page = max(0, stats.pdf_processing_avg_sec_per_page - 
                                       stats.pdf_parsing_avg_sec_per_page - 
                                       stats.embedding_avg_sec_per_page)
        mapping_estimated_sec = pdf_page_count * mapping_avg_sec_per_page
        # 저장 시간은 매우 짧으므로 고정값으로 추정 (예: 5초)
        save_estimated_sec = 5.0
        sequential_estimated_sec = mapping_estimated_sec + save_estimated_sec
        
        # 총 예상 시간 = 병렬 그룹 1 + 병렬 그룹 2 + 순차 처리
        estimated_time_sec = group1_estimated_sec + group2_estimated_sec + sequential_estimated_sec
        
        # Lecture 모델에 저장
        lecture.estimated_time_sec = int(estimated_time_sec)
        lecture.save()
        
        print(f"ETR 계산 완료: {estimated_time_sec:.0f}초")
        print(f"  - 병렬 그룹 1 (STT vs PDF 파싱): {group1_estimated_sec:.0f}초 (STT: {stt_estimated_sec:.0f}초, PDF 파싱: {pdf_parsing_estimated_sec:.0f}초)")
        print(f"  - 병렬 그룹 2 (요약 vs 임베딩): {group2_estimated_sec:.0f}초 (요약: {summary_estimated_sec:.0f}초, 임베딩: {embedding_estimated_sec:.0f}초)")
        print(f"  - 순차 처리 (매핑+저장): {sequential_estimated_sec:.0f}초")
        print(f"  (오디오: {audio_duration_min:.1f}분, PDF: {pdf_page_count}페이지)")
        
    except Exception as e:
        print(f"ETR 계산 실패: {e}")
        try:
            lecture = Lecture.objects.get(id=lecture_id)
            lecture.estimated_time_sec = 0
            lecture.save()
        except:
            pass

@shared_task(bind=True, max_retries=0)
def start_process_from_url_task(self, lecture_id):
    """
    YouTube URL에서 오디오를 다운로드하고 처리 파이프라인을 시작하는 태스크
    
    Args:
        self: Celery 작업 인스턴스 (bind=True로 인해 자동 전달)
        lecture_id: 처리할 강의 ID
    """
    try:
        lecture = Lecture.objects.get(id=lecture_id)
        
        if not lecture.youtube_url:
            raise Exception("YouTube URL이 없습니다.")
        
        print(f"[YouTube 다운로드] 시작: {lecture.youtube_url}")
        
        # 다운로드할 파일 경로 생성
        # audio_upload_path 함수와 동일한 로직 사용
        user_dir = os.path.join(settings.MEDIA_ROOT, str(lecture.user.id))
        os.makedirs(user_dir, exist_ok=True)
        
        # 파일명에서 특수문자 제거 (안전한 파일명 생성)
        safe_lecture_name = "".join(c for c in lecture.lecture_name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_lecture_name = safe_lecture_name.replace(' ', '_')
        
        # 임시 파일명 생성 (확장자는 yt-dlp가 결정)
        temp_filename = f"{safe_lecture_name}_audio_temp"
        temp_path = os.path.join(user_dir, temp_filename)
        
        # yt-dlp를 사용하여 오디오만 다운로드 (실시간 출력 파싱)
        # --extract-audio: 오디오만 추출
        # --audio-format mp3: MP3 형식으로 변환
        # --output: 출력 파일 경로 (확장자 없이, yt-dlp가 자동으로 추가)
        # --progress: 진행률 표시 (기본값이지만 명시)
        try:
            process = subprocess.Popen(
                [
                    'yt-dlp',
                    '--extract-audio',
                    '--audio-format', 'mp3',
                    '--output', temp_path + '.%(ext)s',
                    '--no-playlist',
                    '--newline',  # 실시간 출력을 위해 줄바꿈 강제
                    lecture.youtube_url
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # 라인 버퍼링
                universal_newlines=True
            )
            
            # ETA 파싱을 위한 정규식
            # 예: [download]  42.2% of   46.79MiB at    2.27MiB/s ETA 00:11
            eta_pattern = re.compile(r'ETA\s+(\d{2}):(\d{2})')
            percent_pattern = re.compile(r'\[download\]\s+(\d+\.?\d*)%')
            
            # 실시간 출력 파싱
            for line in iter(process.stdout.readline, ''):
                if not line:
                    break
                
                line = line.strip()
                print(f"{line}")  # 로그 출력
                
                # ETA 추출 (MM:SS 형식) - 단계 0에서는 ETA를 표시하지 않으므로 업데이트하지 않음
                # eta_match = eta_pattern.search(line)
                # if eta_match:
                #     minutes = int(eta_match.group(1))
                #     seconds = int(eta_match.group(2))
                #     eta_sec = minutes * 60 + seconds
                #     
                #     # Lecture 모델에 ETA 업데이트
                #     Lecture.objects.filter(id=lecture_id).update(youtube_download_eta_sec=eta_sec)
                
                # 진행률 추출 (선택적, 로깅용)
                percent_match = percent_pattern.search(line)
                if percent_match:
                    percent = float(percent_match.group(1))
                    # 필요시 진행률도 저장할 수 있음
            
            # 프로세스 완료 대기
            return_code = process.wait(timeout=600)  # 10분 타임아웃
            
            if return_code != 0:
                raise Exception(f"YouTube 다운로드 실패 (반환 코드: {return_code})")
                
        except subprocess.TimeoutExpired:
            process.kill()
            raise Exception("YouTube 다운로드가 타임아웃되었습니다. (10분 초과)")
        except Exception as e:
            if 'process' in locals():
                process.kill()
            raise Exception(f"YouTube 다운로드 실패: {str(e)}")
        
        # 다운로드된 파일 찾기 (yt-dlp가 확장자를 추가했을 수 있음)
        downloaded_file = None
        possible_extensions = ['mp3', 'm4a', 'webm', 'opus']
        for ext in possible_extensions:
            candidate = f"{temp_path}.{ext}"
            if os.path.exists(candidate):
                downloaded_file = candidate
                break
        
        if not downloaded_file or not os.path.exists(downloaded_file):
            raise Exception("다운로드된 오디오 파일을 찾을 수 없습니다.")
        
        # 최종 파일명 생성 (audio_upload_path와 동일한 형식)
        final_filename = f"{safe_lecture_name}_audio.mp3"
        final_path = os.path.join(user_dir, final_filename)
        
        # 파일이 이미 존재하면 삭제
        if os.path.exists(final_path):
            os.remove(final_path)
        
        # 임시 파일을 최종 경로로 이동
        os.rename(downloaded_file, final_path)
        
        # 상대 경로 계산 (MEDIA_ROOT 기준)
        relative_path = os.path.join(str(lecture.user.id), final_filename)
        
        # Lecture 모델의 audio_file 필드 업데이트
        lecture.audio_file.name = relative_path
        lecture.save()
        
        print(f"[YouTube 다운로드] 파일 저장 완료: {final_path}")
        
        # 기존 처리 파이프라인 시작
        print(f"[YouTube 다운로드] 처리 파이프라인 시작...")
        process_lecture_task.delay(lecture_id)
        
        # ETR 계산 태스크 호출 (비동기, 빠른 계산)
        calculate_etr_task.delay(lecture_id)
        
    except Exception as e:
        print(f"[YouTube 다운로드] 실패: {e}")
        error_message = str(e)
        # 실패 상태로 표시
        mark_lecture_as_failed(lecture_id, error_message)
        # 예외를 다시 발생시켜 Celery가 실패를 기록하도록 함
        raise


# Celery 시그널 핸들러: 작업 실패 시 강의 상태를 실패로 업데이트 (보조적 역할)
# 주의: 작업 내부에서 이미 실패 처리를 하고 있으므로, 이 핸들러는 추가 보호 역할만 합니다.
@task_failure.connect
def task_failure_handler(sender=None, task_id=None, exception=None, traceback=None, einfo=None, **kwargs):
    """
    Celery 작업이 실패할 때 호출되는 시그널 핸들러
    process_lecture_task가 실패한 경우 강의 상태를 'failed'로 업데이트합니다.
    """
    try:
        # 작업 이름 확인
        task_name = None
        if sender:
            if hasattr(sender, 'name'):
                task_name = sender.name
            elif isinstance(sender, str):
                task_name = sender
        
        if task_name and ('process_lecture_task' in task_name or 'start_process_from_url_task' in task_name):
            # Celery 결과 백엔드에서 작업 정보 가져오기 시도
            try:
                from celery.result import AsyncResult
                from config.celery import app as celery_app
                
                result = AsyncResult(task_id, app=celery_app)
                # 작업 메타데이터에서 인자 가져오기
                if hasattr(result, 'result') and result.result:
                    # 결과가 딕셔너리 형태인 경우
                    pass
                
                # 작업 요청 정보에서 인자 가져오기
                if hasattr(result, 'request') and result.request:
                    if hasattr(result.request, 'args') and result.request.args:
                        lecture_id = result.request.args[0]
                        error_message = str(exception) if exception else "작업이 예기치 않게 종료되었습니다."
                        mark_lecture_as_failed(lecture_id, error_message)
                        return
            except Exception:
                # 시그널 핸들러 실패는 무시 (작업 내부에서 이미 처리됨)
                pass
    except Exception as e:
        # 시그널 핸들러 오류는 무시 (작업 내부에서 이미 처리됨)
        pass

# Celery 워커 시작 시 자동으로 오래된 작업 체크
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """
    Celery 워커가 준비되었을 때 호출되는 시그널 핸들러
    워커 시작 시 오래된 '처리 중' 작업을 자동으로 감지하고 실패로 표시합니다.
    """
    try:
        print("[Celery 워커 시작] 오래된 작업을 자동으로 체크합니다...")
        check_and_mark_stuck_tasks(minutes=18, dry_run=False)
    except Exception as e:
        # 워커 시작 시 체크 실패는 무시 (워커는 계속 실행되어야 함)
        print(f"[Celery 워커 시작] 오래된 작업 체크 중 오류 발생 (무시됨): {e}")