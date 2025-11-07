from celery import shared_task
from .models import Lecture, PdfChunk, Mapping, ProcessingStats
from .services import (
    init_gemini_models, init_chromadb_client, 
    process_audio, process_pdf, get_summary_from_gemini,
    embed_and_store, create_semantic_mappings
)
import time
import subprocess
import os

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

@shared_task
def process_lecture_task(lecture_id):
    try:
        lecture = Lecture.objects.get(id=lecture_id)
        models = init_gemini_models()
        chroma_client = init_chromadb_client()

        # Streamlit의 upload_view 로직을 그대로 가져옴
        start_time = time.time()
        
        # 오디오 길이와 PDF 페이지 수 계산 (통계 업데이트용)
        audio_path = lecture.audio_file.path
        pdf_path = lecture.pdf_file.path
        
        try:
            audio_duration_sec = get_audio_duration_fast(audio_path)
            audio_duration_min = audio_duration_sec / 60.0 if audio_duration_sec else 0
        except Exception as e:
            print(f"오디오 길이 계산 실패: {e}")
            audio_duration_min = 0
        
        # 1. STT 및 PDF 파싱
        print("1/5: STT 및 PDF 처리 시작...")
        Lecture.objects.filter(id=lecture_id).update(current_step=1)
        
        # STT 시간 측정 (오디오 길이 기반 평균 계산용)
        stt_start_time = time.time()
        full_script_ts, script_text_only = process_audio(audio_path, models['flash'])
        if full_script_ts is None or script_text_only is None:
            raise Exception("STT 처리 실패: 오디오 파일을 텍스트로 변환할 수 없습니다.")
        stt_elapsed_sec = time.time() - stt_start_time
        
        # PDF 파싱 시간 측정 (PDF 처리 평균 계산용)
        pdf_parse_start_time = time.time()
        pdf_texts = process_pdf(pdf_path)
        if not pdf_texts:
            raise Exception("PDF 파싱 실패: PDF 파일을 읽을 수 없습니다.")
        pdf_parse_elapsed_sec = time.time() - pdf_parse_start_time
        pdf_page_count = len(pdf_texts) if pdf_texts else 0
        
        print("1/5: STT 및 PDF 처리 완료")
        
        # 2. 요약
        print("2/5: 스크립트 요약 시작...")
        Lecture.objects.filter(id=lecture_id).update(current_step=2)
        summary_start_time = time.time()
        
        # 타임스탬프가 포함된 전체 스크립트를 요약 생성에 사용
        summary_json = get_summary_from_gemini(models['flash'], full_script_ts)
        if summary_json is None:
            raise Exception("요약 생성 실패: 스크립트 요약을 생성할 수 없습니다.")
        
        summary_elapsed_sec = time.time() - summary_start_time
        print("2/5: 스크립트 요약 완료")
        
        # 3. 임베딩
        print("3/5: 임베딩 및 벡터 DB 저장 시작...")
        Lecture.objects.filter(id=lecture_id).update(current_step=3)
        embed_start_time = time.time()
        
        embed_and_store(lecture.id, pdf_texts, full_script_ts, models['embedding'], chroma_client)
        
        embed_elapsed_sec = time.time() - embed_start_time
        print("3/5: 임베딩 완료")

        # 4. 매핑
        print("4/5: 의미 기반 매핑 시작...")
        Lecture.objects.filter(id=lecture_id).update(current_step=4)
        mapping_start_time = time.time()
        
        mappings_to_create = create_semantic_mappings(lecture.id, summary_json, models['embedding'], chroma_client)
        
        mapping_elapsed_sec = time.time() - mapping_start_time
        print("4/5: 매핑 완료")
        
        # PDF 처리 시간 = 파싱 + 임베딩 + 매핑
        pdf_processing_elapsed_sec = pdf_parse_elapsed_sec + embed_elapsed_sec + mapping_elapsed_sec
        
        # 5. DB에 결과 저장
        lecture = Lecture.objects.get(id=lecture_id)
        lecture.current_step = 5
        lecture.full_script = full_script_ts
        lecture.summary_json = summary_json
        lecture.status = 'completed' # 상태를 '완료'로 변경
        lecture.save()

        # 6. PdfChunk 및 Mapping 모델에도 저장 
        for page_num, content in pdf_texts:
            PdfChunk.objects.create(lecture=lecture, page_num=page_num, content=content)
        
        # Mapping 정보 대량 저장 (bulk_create)
        Mapping.objects.bulk_create(
            [Mapping(lecture=lecture, **m) for m in mappings_to_create]
        )

        print(f"처리 완료 (총 {(time.time() - start_time):.2f}초)")
        
        # 7. ProcessingStats 업데이트 (이동 평균 방식)
        try:
            stats = ProcessingStats.get_or_create_singleton()
            
            # STT 평균 업데이트 (1분당 초)
            if audio_duration_min > 0:
                stt_sec_per_min = stt_elapsed_sec / audio_duration_min
                # 이동 평균: 기존 평균과 새 값의 가중 평균 (기존 50%, 새 50%)
                stats.audio_stt_avg_sec_per_min = stats.audio_stt_avg_sec_per_min * 0.5 + stt_sec_per_min * 0.5
            
            # PDF 처리 평균 업데이트 (1페이지당 초)
            if pdf_page_count > 0:
                pdf_sec_per_page = pdf_processing_elapsed_sec / pdf_page_count
                # 이동 평균: 기존 평균과 새 값의 가중 평균 (기존 50%, 새 50%)
                stats.pdf_processing_avg_sec_per_page = stats.pdf_processing_avg_sec_per_page * 0.5 + pdf_sec_per_page * 0.5
            
            # 요약 평균 업데이트 (고정값)
            # 이동 평균: 기존 평균과 새 값의 가중 평균 (기존 50%, 새 50%)
            stats.summary_avg_sec = stats.summary_avg_sec * 0.5 + summary_elapsed_sec * 0.5
            
            stats.save()
            print(f"ProcessingStats 업데이트 완료: STT={stats.audio_stt_avg_sec_per_min:.2f}초/분, PDF={stats.pdf_processing_avg_sec_per_page:.2f}초/페이지, 요약={stats.summary_avg_sec:.2f}초")
        except Exception as e:
            print(f"ProcessingStats 업데이트 실패: {e}")

    except Exception as e:
        print(f"작업 실패: {e}")
        if 'lecture' in locals():
            lecture.status = 'failed' # 상태를 '실패'로 변경
            lecture.save()

@shared_task
def calculate_etr_task(lecture_id):
    """
    ETR(예상 소요 시간)을 빠르게 계산하는 별도 태스크
    업로드 시 즉시 리다이렉트하기 위해 비동기로 실행됩니다.
    """
    try:
        lecture = Lecture.objects.get(id=lecture_id)
        
        # 오디오 길이 계산 (빠른 방법 사용)
        try:
            audio_path = lecture.audio_file.path
            audio_duration_sec = get_audio_duration_fast(audio_path)
            audio_duration_min = audio_duration_sec / 60.0 if audio_duration_sec else 0
        except Exception as e:
            print(f"ETR 계산: 오디오 길이 계산 실패: {e}")
            audio_duration_min = 0
        
        # PDF 페이지 수 계산
        try:
            pdf_path = lecture.pdf_file.path
            pdf_texts = process_pdf(pdf_path)
            pdf_page_count = len(pdf_texts) if pdf_texts else 0
        except Exception as e:
            print(f"ETR 계산: PDF 페이지 수 계산 실패: {e}")
            pdf_page_count = 0
        
        # ProcessingStats에서 평균값 가져오기
        stats = ProcessingStats.get_or_create_singleton()
        
        # ETR 계산: (오디오 길이(분) * STT 평균) + (PDF 페이지 수 * PDF 처리 평균) + 요약 평균
        estimated_time_sec = (
            audio_duration_min * stats.audio_stt_avg_sec_per_min +
            pdf_page_count * stats.pdf_processing_avg_sec_per_page +
            stats.summary_avg_sec
        )
        
        # Lecture 모델에 저장
        lecture.estimated_time_sec = int(estimated_time_sec)
        lecture.save()
        
        print(f"ETR 계산 완료: {estimated_time_sec:.0f}초 (오디오: {audio_duration_min:.1f}분, PDF: {pdf_page_count}페이지)")
        
    except Exception as e:
        print(f"ETR 계산 실패: {e}")
        try:
            lecture = Lecture.objects.get(id=lecture_id)
            lecture.estimated_time_sec = 0
            lecture.save()
        except:
            pass