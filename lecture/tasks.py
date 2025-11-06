from celery import shared_task
from django.db import transaction
from .models import Lecture, PdfChunk, Mapping
from .services import (
    init_gemini_models, init_chromadb_client, 
    process_audio, process_pdf, get_summary_from_gemini,
    embed_and_store, create_semantic_mappings
)
import time

@shared_task
def process_lecture_task(lecture_id):
    try:
        lecture = Lecture.objects.get(id=lecture_id)
        models = init_gemini_models()
        chroma_client = init_chromadb_client()

        # Streamlit의 upload_view 로직을 그대로 가져옴
        start_time = time.time()
        
        # 1. STT 및 PDF 파싱
        print("1/5: STT 및 PDF 처리 시작...")
        # select_for_update로 락을 걸고 업데이트하여 즉시 반영 보장
        with transaction.atomic():
            lecture = Lecture.objects.select_for_update().get(id=lecture_id)
            lecture.current_step = 1
            lecture.save(update_fields=['current_step'])
        # 업데이트 확인
        updated_lecture = Lecture.objects.get(id=lecture_id)
        print(f"[DEBUG] current_step 업데이트 확인: {updated_lecture.current_step}")
        audio_path = lecture.audio_file.path
        pdf_path = lecture.pdf_file.path
        
        full_script_ts, script_text_only = process_audio(audio_path, models['flash'])
        if full_script_ts is None or script_text_only is None:
            raise Exception("STT 처리 실패: 오디오 파일을 텍스트로 변환할 수 없습니다.")
        
        pdf_texts = process_pdf(pdf_path)
        if not pdf_texts:
            raise Exception("PDF 파싱 실패: PDF 파일을 읽을 수 없습니다.")
        
        print("1/5: STT 및 PDF 처리 완료")
        
        # 2. 요약
        print("2/5: 스크립트 요약 시작...")
        with transaction.atomic():
            lecture = Lecture.objects.select_for_update().get(id=lecture_id)
            lecture.current_step = 2
            lecture.save(update_fields=['current_step'])
        updated_lecture = Lecture.objects.get(id=lecture_id)
        print(f"[DEBUG] current_step 업데이트 확인: {updated_lecture.current_step}")
        summary_json = get_summary_from_gemini(models['flash'], script_text_only)
        if summary_json is None:
            raise Exception("요약 생성 실패: 스크립트 요약을 생성할 수 없습니다.")
        print("2/5: 스크립트 요약 완료")
        
        # 3. 임베딩
        print("3/5: 임베딩 및 벡터 DB 저장 시작...")
        with transaction.atomic():
            lecture = Lecture.objects.select_for_update().get(id=lecture_id)
            lecture.current_step = 3
            lecture.save(update_fields=['current_step'])
        updated_lecture = Lecture.objects.get(id=lecture_id)
        print(f"[DEBUG] current_step 업데이트 확인: {updated_lecture.current_step}")
        embed_and_store(lecture.id, pdf_texts, full_script_ts, models['embedding'], chroma_client)
        print("3/5: 임베딩 완료")

        # 4. 매핑
        print("4/5: 의미 기반 매핑 시작...")
        with transaction.atomic():
            lecture = Lecture.objects.select_for_update().get(id=lecture_id)
            lecture.current_step = 4
            lecture.save(update_fields=['current_step'])
        updated_lecture = Lecture.objects.get(id=lecture_id)
        print(f"[DEBUG] current_step 업데이트 확인: {updated_lecture.current_step}")
        mappings_to_create = create_semantic_mappings(lecture.id, summary_json, models['embedding'], chroma_client)
        print("4/5: 매핑 완료")
        
        # 5. DB에 결과 저장
        with transaction.atomic():
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

    except Exception as e:
        print(f"작업 실패: {e}")
        if 'lecture' in locals():
            lecture.status = 'failed' # 상태를 '실패'로 변경
            lecture.save()