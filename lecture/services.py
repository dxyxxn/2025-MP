import google.generativeai as genai
import fitz  # PyMuPDF
import json
import time
import re
import logging
import chromadb
import ollama
import base64
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from django.conf import settings

# Django 로거 설정
logger = logging.getLogger(__name__)

# --- 0. 모델 및 클라이언트 초기화 ---
# (이 함수들은 tasks.py에서 호출됩니다)
def init_gemini_models():
    """Gemini 모델 객체들을 초기화하고 딕셔너리로 반환"""
    print("Initializing Gemini models...")
    try:
        genai.configure(api_key=settings.GEMINI_API_KEY)
        models = {
            'flash': genai.GenerativeModel(settings.MODEL_FLASH),
            # 'pro': genai.GenerativeModel(settings.MODEL_PRO), # Pro 모델이 있다면 주석 해제
            'embedding': settings.MODEL_EMBEDDING
        }
        print("Gemini models initialized.")
        return models
    except Exception as e:
        logger.error(f"Error initializing Gemini: {e}")
        raise

def init_chromadb_client():
    """ChromaDB 클라이언트 초기화 (Persistent)"""
    print("Connecting to ChromaDB...")
    try:
        client = chromadb.PersistentClient(path=settings.CHROMA_PATH)
        print("ChromaDB initialized.")
        return client
    except Exception as e:
        logger.error(f"Error initializing ChromaDB: {e}")
        raise

def init_ollama_client():
    """Ollama 클라이언트 초기화"""
    print("Initializing Ollama client...")
    try:
        # Ollama 클라이언트 초기화
        # OLLAMA_BASE_URL에서 호스트 추출 (http://localhost:11434 -> localhost:11434)
        base_url = settings.OLLAMA_BASE_URL
        if base_url.startswith('http://'):
            base_url = base_url[7:]
        elif base_url.startswith('https://'):
            base_url = base_url[8:]
        
        client = ollama.Client(host=base_url)
        
        # 모델이 존재하는지 확인
        try:
            models_response = client.list()
            # ollama 패키지의 반환 형식: ListResponse 객체 (models 속성 포함)
            model_list = []
            if hasattr(models_response, 'models'):
                model_list = models_response.models
            elif isinstance(models_response, dict) and 'models' in models_response:
                model_list = models_response['models']
            elif isinstance(models_response, list):
                model_list = models_response
            
            # 모델 이름 추출
            model_names = []
            for m in model_list:
                if hasattr(m, 'model'):  # Model 객체인 경우
                    model_names.append(m.model)
                elif isinstance(m, dict):
                    model_names.append(m.get('name', m.get('model', str(m))))
                else:
                    model_names.append(str(m))
            
            # 모델 이름 비교: 'bakllava:latest' 형식도 'bakllava'와 매칭되도록 처리
            model_found = any(
                settings.OLLAMA_MODEL in name or name.startswith(settings.OLLAMA_MODEL + ':')
                for name in model_names
            )
            if not model_found:
                logger.warning(f"Ollama 모델 '{settings.OLLAMA_MODEL}'이 설치되지 않았습니다. 사용 가능한 모델: {model_names}")
                print(f"경고: 모델 '{settings.OLLAMA_MODEL}'을 찾을 수 없습니다. 'ollama pull {settings.OLLAMA_MODEL}' 명령으로 설치하세요.")
        except Exception as e:
            logger.warning(f"Ollama 모델 목록 확인 실패: {e}")
        
        print(f"Ollama client initialized (model: {settings.OLLAMA_MODEL}).")
        return client
    except Exception as e:
        logger.error(f"Error initializing Ollama: {e}")
        raise

# --- 1. STT (Gemini API) ---
def process_audio(_audio_path, _model_flash):
    """Gemini API를 사용해 오디오 파일에서 스크립트 추출"""
    
    print(f"Uploading audio to Gemini: {_audio_path}...")
    try:
        audio_file = genai.upload_file(path=_audio_path)
    except Exception as e:
        logger.error(f"오디오 파일 업로드 실패: {e}")
        return None, None

    print("Transcribing with Gemini Flash...")
    
    prompt = [
        "다음은 강의 오디오 파일입니다. 이 파일을 한국어 텍스트로 변환해 주세요.",
        audio_file,
        "다음의 요구사항을 반드시 지켜주세요:",
        "1. 한국어 스크립트를 작성해 주세요.",
        "2. 음성 녹음을 빼먹지 말고 변환해 주세요",
        "3. 각 문장이나 문단 앞에 해당하는 시간을 [MM:SS] 형식으로 표시해 주세요.",
        "4. 예시: [00:15] 안녕하세요. 오늘은 ~~에 대해 배워보겠습니다.",
        "5. 스크립트 외의 다른 답변은 하지 말아주세요."
    ]
    
    try:
        # 타임아웃 10분(600초) 설정 (Pro 모델 사용 시 900초 권장)
        response = _model_flash.generate_content(prompt, request_options={"timeout": 600})
        
        full_script_ts = response.text
        
        # 'text_only' 스크립트 생성 (요약 모델 입력용)
        script_text_only = re.sub(r'\[\d{2}:\d{2}\s*-\s*\d{2}:\d{2}\]\s*', '', full_script_ts)
        
        if not script_text_only.strip():
            script_text_only = full_script_ts
            
        print("Gemini transcription complete.")
        return full_script_ts, script_text_only

    except Exception as e:
        logger.error(f"Gemini STT 처리 중 오류 발생: {e}")
        genai.delete_file(audio_file.name)
        return None, None
    finally:
        # STT 작업 완료 후 업로드된 파일 삭제 (오류 여부와 관계없이)
        try:
            print(f"Deleting uploaded audio file: {audio_file.name}")
            genai.delete_file(audio_file.name)
        except Exception as e:
            logger.warning(f"Failed to delete uploaded file {audio_file.name}: {e}")


# --- 2. PDF 파싱 (Ollama bakllava 모델 사용) ---
# 이미지 분석용 영어 프롬프트 (test_bakllava_pdf.py와 동일)
IMAGE_DESCRIPTION_PROMPT = """Describe this image in detail in English. 
Include all visible text, diagrams, charts, formulas, and visual elements.
If there are any labels, captions, or annotations, include them in your description.
Be thorough and accurate in describing what you see."""

def extract_images_from_page(page, pdf_doc):
    """PDF 페이지에서 이미지 객체들을 추출"""
    images = []
    try:
        image_list = page.get_images(full=True)
        for img_index, img in enumerate(image_list):
            try:
                xref = img[0]
                base_image = pdf_doc.extract_image(xref)
                image_bytes = base_image["image"]
                image_ext = base_image["ext"]
                
                # base64로 인코딩
                img_base64 = base64.b64encode(image_bytes).decode('utf-8')
                images.append({
                    'index': img_index,
                    'base64': img_base64,
                    'ext': image_ext,
                    'width': base_image.get('width', 0),
                    'height': base_image.get('height', 0)
                })
            except Exception as e:
                logger.warning(f"이미지 추출 실패 (인덱스 {img_index}): {e}")
                continue
    except Exception as e:
        logger.warning(f"페이지 이미지 목록 가져오기 실패: {e}")
    return images

def process_single_page_with_ollama(page_num, page, pdf_doc, ollama_client):
    """단일 PDF 페이지 처리: 텍스트 추출 + 이미지 분석 (test_bakllava_pdf.py와 동일한 방식)"""
    try:
        # 1. PyMuPDF로 텍스트 추출 (정확하고 빠름)
        page_text = page.get_text("text").strip()
        
        # 2. 페이지에서 이미지 추출
        page_images = extract_images_from_page(page, pdf_doc)
        
        # 3. 이미지가 있으면 Ollama로 각 이미지 분석
        image_descriptions = []
        if page_images:
            for img_info in page_images:
                try:
                    response = ollama_client.generate(
                        model=settings.OLLAMA_MODEL,
                        prompt=IMAGE_DESCRIPTION_PROMPT,
                        images=[img_info['base64']],
                        options={
                            'temperature': 0.1,
                        }
                    )
                    
                    if hasattr(response, 'response'):
                        img_description = response.response.strip()
                    elif isinstance(response, dict):
                        img_description = response.get('response', '').strip()
                    else:
                        img_description = str(response).strip()
                    
                    if img_description:
                        image_descriptions.append(f"[Image {img_info['index'] + 1}]: {img_description}")
                except Exception as e:
                    logger.warning(f"이미지 {img_info['index'] + 1} 분석 실패: {e}")
                    image_descriptions.append(f"[Image {img_info['index'] + 1}]: Error analyzing image - {str(e)}")
        
        # 4. 텍스트와 이미지 설명 결합
        combined_content = []
        if page_text:
            combined_content.append("=== Text Content ===")
            combined_content.append(page_text)
        
        if image_descriptions:
            if combined_content:
                combined_content.append("\n")
            combined_content.append("=== Image Descriptions ===")
            combined_content.extend(image_descriptions)
        
        final_text = "\n".join(combined_content) if combined_content else ""
        
        return (page_num + 1, final_text)
    except Exception as e:
        logger.error(f"페이지 {page_num + 1} 처리 중 오류 발생: {e}")
        return (page_num + 1, "")

def get_pdf_page_count(_pdf_path):
    """PDF의 페이지 수만 빠르게 계산 (ETR 계산용)"""
    try:
        doc = fitz.open(_pdf_path)
        page_count = len(doc)
        doc.close()
        return page_count
    except Exception as e:
        logger.error(f"PDF 페이지 수 계산 실패: {e}")
        return 0

def process_pdf(_pdf_path, ollama_client=None):
    """PDF를 페이지별로 파싱하고 Ollama bakllava 모델로 이미지와 텍스트 추출
    (test_bakllava_pdf.py와 동일한 방식: PyMuPDF 텍스트 추출 + 이미지 분석)"""
    print(f"Parsing PDF with Ollama: {_pdf_path}...")
    
    if ollama_client is None:
        ollama_client = init_ollama_client()
    
    try:
        doc = fitz.open(_pdf_path)
        total_pages = len(doc)
        pdf_texts = []
        
        # 배치 크기 설정
        batch_size = settings.OLLAMA_BATCH_SIZE
        
        print(f"총 {total_pages}페이지를 배치 크기 {batch_size}로 처리합니다...")
        print("(텍스트는 즉시 추출, 이미지가 있는 페이지만 Ollama로 분석)")
        
        # 배치 단위로 처리
        for batch_start in tqdm(range(0, total_pages, batch_size), desc="PDF 페이지 배치 처리"):
            batch_end = min(batch_start + batch_size, total_pages)
            batch_pages = list(range(batch_start, batch_end))
            
            # 병렬 처리로 배치 내 페이지들 처리
            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = {
                    executor.submit(process_single_page_with_ollama, page_num, doc[page_num], doc, ollama_client): page_num
                    for page_num in batch_pages
                }
                
                # 완료된 작업부터 결과 수집
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        pdf_texts.append(result)  # 빈 텍스트도 포함
                    except Exception as e:
                        page_num = futures[future]
                        logger.error(f"페이지 {page_num + 1} 처리 실패: {e}")
                        pdf_texts.append((page_num + 1, ""))
        
        doc.close()
        
        # 페이지 번호 순으로 정렬
        pdf_texts.sort(key=lambda x: x[0])
        
        print(f"PDF parsing complete. {len(pdf_texts)} pages processed.")
        
        # 통계 정보 출력 (test_bakllava_pdf.py와 동일한 형식)
        if pdf_texts:
            total_chars = sum(len(text) for _, text in pdf_texts)
            avg_chars_per_page = total_chars / len(pdf_texts) if pdf_texts else 0
            pages_with_text = sum(1 for _, text in pdf_texts if text.strip())
            pages_with_text_ratio = pages_with_text / len(pdf_texts) * 100 if pdf_texts else 0
            
            print("== PDF 처리 결과 통계 ==")
            print(f"총 페이지 수: {len(pdf_texts)}")
            print(f"텍스트가 있는 페이지: {pages_with_text}")
            print(f"텍스트가 있는 페이지 비율: {pages_with_text_ratio:.1f}%")
            print(f"총 문자 수: {total_chars:,}자")
            print(f"페이지당 평균 문자 수: {avg_chars_per_page:,.0f}자")
        
        return pdf_texts
    except Exception as e:
        logger.error(f"PDF 파싱 중 오류 발생: {e}")
        return []

# --- 3. 요약 및 구조화 (Gemini Flash) ---
def get_summary_from_gemini(_model_flash, script_text_with_timestamp):
    print("Generating summary with Gemini Flash...")
    
    # script_text가 너무 길면 Gemini 입력 제한에 걸릴 수 있음 (약 32k 토큰)
    # 여기서는 원본처럼 전체를 보내지만, 실제로는 청크로 나누거나 앞부분을 잘라야 할 수 있음
    truncated_script = script_text_with_timestamp # 예시: 3만자 제한
    
    prompt = f"""
    다음은 대학 강의 스크립트입니다. 이 스크립트의 전체 내용을 파악한 뒤,
    '소주제(sub-topic)' 단위로 명확하게 나누어 주세요.
    
    각 소주제에 대해 다음 정보를 포함하는 JSON 형식으로 출력해 주세요:
    1. 'topic': 소주제의 핵심 제목 (예: "텐서 병렬 처리의 개념")
    2. 'summary': 해당 소주제의 내용을 2-3문장으로 요약
    3. 'original_segment': 해당 소주제가 시작되는 원본 스크립트의 핵심 문장
    4. 'timestamp': 해당 소주제가 시작되는 시간 (스크립트에서 [MM:SS] 형식으로 표시된 타임스탬프를 찾아서 포함)

    [강의 스크립트 시작]
    {truncated_script}
    [...중략...]
    [강의 스크립트 끝]

    JSON 형식 예시:
    {{
      "summary_list": [
        {{
          "topic": "소주제 제목 1",
          "summary": "소주제 1의 요약 내용입니다.",
          "original_segment": "원본 스크립트의 핵심 문장...",
          "timestamp": "[05:30]"
        }}
      ]
    }}

    반드시 유효한 JSON 객체만 응답해 주세요.
    타임스탬프는 스크립트에서 해당 소주제가 시작되는 부분의 [MM:SS] 형식 타임스탬프를 찾아서 포함해 주세요.
    """
    
    try:
        response = _model_flash.generate_content(prompt)
        json_str = response.text.strip().lstrip("```json").rstrip("```")
        summary_data = json.loads(json_str)
        print("Summary generation complete.")
        return json.dumps(summary_data, indent=2) # JSON 문자열로 반환
    except Exception as e:
        logger.error(f"요약 생성 중 오류 발생: {e}")
        return None

# --- 4. 임베딩 및 벡터 DB 저장 ---
def embed_and_store(lecture_id, pdf_texts, script_text, _model_embedding, _chroma_client):
    print("Starting embedding and storage...")
    collection_name = f"lecture_{lecture_id}"
    
    try:
        if _chroma_client.get_collection(name=collection_name):
            _chroma_client.delete_collection(name=collection_name)
    except Exception:
        pass # 컬렉션이 없으면 오류 발생, 정상임

    collection = _chroma_client.get_or_create_collection(name=collection_name)
    
    documents = []
    metadatas = []
    ids = []
    
    # PDF 청크 (pdf_texts는 [(page_num, content), ...])
    for page_num, content in pdf_texts:
        documents.append(content)
        metadatas.append({"source": "pdf", "page": page_num, "lecture_id": lecture_id})
        ids.append(f"pdf_{lecture_id}_{page_num}")

    # 스크립트 청크 (10줄 단위)
    script_lines = script_text.split('\n')
    chunk_size = 10
    for i in range(0, len(script_lines), chunk_size):
        chunk = "\n".join(script_lines[i:i+chunk_size])
        if chunk.strip():
            timestamp = script_lines[i].split(']')[0] + "]" if script_lines[i] else "[00:00]"
            documents.append(chunk)
            metadatas.append({"source": "script", "timestamp": timestamp, "lecture_id": lecture_id})
            ids.append(f"script_{lecture_id}_{i}")
            
    # 배치 임베딩 및 저장
    batch_size = 100 
    for i in tqdm(range(0, len(documents), batch_size), desc="Embedding Batches"):
        batch_docs = documents[i:i+batch_size]
        batch_ids = ids[i:i+batch_size]
        batch_metadatas = metadatas[i:i+batch_size]
        
        try:
            embeddings = genai.embed_content(
                model=_model_embedding,
                content=batch_docs,
                task_type="retrieval_document"
            )
            collection.add(
                embeddings=embeddings['embedding'],
                documents=batch_docs,
                metadatas=batch_metadatas,
                ids=batch_ids
            )
        except Exception as e:
            logger.error(f"Error during embedding batch {i}: {e}")
            
    print("Embedding and storage complete.")
    # (반환값 없음. ChromaDB에 저장하는 것이 목적)

# --- 5. 의미 기반 자동 매핑 ---
def create_semantic_mappings(lecture_id, summary_json, _model_embedding, _chroma_client):
    print("Creating semantic mappings...")
    mappings_to_create = [] # DB에 저장할 데이터를 리스트로 반환
    collection_name = f"lecture_{lecture_id}"
    
    try:
        collection = _chroma_client.get_collection(name=collection_name)
    except Exception as e:
        logger.error(f"ChromaDB 컬렉션 로드 실패: {e}")
        return []

    try:
        summary_data = json.loads(summary_json)
    except Exception as e:
        logger.error(f"Summary JSON 파싱 실패: {e}")
        return []

    for item in tqdm(summary_data.get("summary_list", []), desc="Creating Mappings"):
        topic = item.get("topic")
        summary = item.get("summary")
        if not topic or not summary:
            continue
            
        query_text = f"주제: {topic}\n요약: {summary}"
        
        try:
            query_embedding = genai.embed_content(
                model=_model_embedding,
                content=[query_text],
                task_type="retrieval_query"
            )['embedding']
            
            results = collection.query(
                query_embeddings=query_embedding,
                n_results=1,
                where={"source": "pdf"}
            )
            
            if results["ids"][0]:
                mapped_doc = results["documents"][0][0]
                mapped_meta = results["metadatas"][0][0]
                mapped_page = mapped_meta["page"]
                
                # DB에 저장할 딕셔너리를 리스트에 추가
                mappings_to_create.append({
                    'lecture_id': lecture_id,
                    'summary_topic': topic,
                    'mapped_pdf_page': mapped_page,
                    'mapped_pdf_content': mapped_doc
                })
                
        except Exception as e:
            logger.error(f"Error mapping topic '{topic}': {e}")
            
    print("Semantic mappings complete.")
    return mappings_to_create # Celery 태스크가 이 리스트를 받아 DB에 저장

# --- 6. 문맥 기반 Q&A (RAG) ---
def get_rag_response(lecture_id, query_text, _model_flash, _model_embedding, _chroma_client):
    print(f"Handling RAG query for lecture {lecture_id}...")
    collection_name = f"lecture_{lecture_id}"
    
    try:
        collection = _chroma_client.get_collection(name=collection_name)
    except Exception as e:
        raise Exception(f"오류: 강의 데이터({lecture_id})에 접근할 수 없습니다. {e}")

    try:
        query_embedding = genai.embed_content(
            model=_model_embedding,
            content=[query_text],
            task_type="retrieval_query"
        )['embedding']
    except Exception as e:
        raise Exception(f"오류: 질문을 임베딩하는 중 실패했습니다. {e}")

    try:
        results = collection.query(
            query_embeddings=query_embedding,
            n_results=5
        )
    except Exception as e:
        raise Exception(f"오류: 벡터 DB에서 검색 중 실패했습니다. {e}")

    context = ""
    sources = []
    if not results["documents"][0]:
        return "관련된 강의 내용을 찾지 못했습니다."

    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        source_info = f"[PDF {meta['page']}p]" if meta["source"] == "pdf" else f"[스크립트 {meta['timestamp']}]"
        context += f"{source_info}\n{doc}\n\n"
        sources.append(source_info)
        
    prompt = f"""
    당신은 강의 내용을 완벽하게 이해한 AI 조교입니다.
    다음 '강의 자료'를 바탕으로 사용자의 '질문'에 대해 명확하고 친절하게 답변해 주세요.
    반드시 제공된 '강의 자료'에 근거하여 답변해야 합니다.

    [강의 자료]
    {context}
    [강의 자료 끝]

    [질문]
    {query_text}

    [답변]
    """
    
    try:
        response = _model_flash.generate_content(prompt)
        unique_sources = " (참고: " + ", ".join(sorted(list(set(sources)))) + ")"
        return response.text + unique_sources
    except Exception as e:
        raise Exception(f"오류: Gemini 답변 생성 중 실패했습니다. {e}")