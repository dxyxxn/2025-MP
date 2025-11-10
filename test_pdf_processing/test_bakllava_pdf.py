#!/usr/bin/env python3
"""
bakllava 모델을 사용한 PDF 처리 테스트 스크립트
입력: media_uploads/26_lecture.pdf
출력: 각 페이지별로 추출된 텍스트를 콘솔 및 파일로 출력
"""

import os
import sys
import json
from pathlib import Path

# Django 설정 로드
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from lecture.services import (
    init_ollama_client
)
from django.conf import settings
import fitz
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import ollama

# 테스트용 영어 프롬프트 (이미지 분석용)
IMAGE_DESCRIPTION_PROMPT = """Describe this image in detail in English. 
Include all visible text, diagrams, charts, formulas, and visual elements.
If there are any labels, captions, or annotations, include them in your description.
Be thorough and accurate in describing what you see."""

def main():
    # PDF 파일 경로
    pdf_path = BASE_DIR / 'media_uploads' / '26_lecture.pdf'
    
    if not pdf_path.exists():
        print(f"❌ 오류: PDF 파일을 찾을 수 없습니다: {pdf_path}")
        return
    
    print("=" * 80)
    print("📄 bakllava 모델을 사용한 PDF 처리 테스트")
    print("=" * 80)
    print(f"📁 입력 파일: {pdf_path}")
    print(f"🤖 사용 모델: {settings.OLLAMA_MODEL}")
    print(f"⚙️  배치 크기: {settings.OLLAMA_BATCH_SIZE}")
    print(f"🌡️  Temperature: 0.1")
    print()
    print("📝 처리 방식:")
    print("-" * 80)
    print("1. PyMuPDF로 페이지 텍스트 추출 (정확하고 빠름)")
    print("2. 페이지에서 이미지 객체 추출")
    print("3. 각 이미지를 Ollama로 분석 (영어)")
    print("4. 텍스트 + 이미지 설명 결합")
    print("-" * 80)
    print()
    print("📝 이미지 분석 프롬프트:")
    print("-" * 80)
    print(IMAGE_DESCRIPTION_PROMPT)
    print("-" * 80)
    print()
    print("=" * 80)
    print()
    
    # Ollama 클라이언트 초기화
    print("🔧 Ollama 클라이언트 초기화 중...")
    try:
        ollama_client = init_ollama_client()
        print("✅ Ollama 클라이언트 초기화 완료\n")
    except Exception as e:
        print(f"❌ Ollama 클라이언트 초기화 실패: {e}")
        return
    
    # PDF 처리 (테스트용: 영어 프롬프트 사용)
    print("📖 PDF 처리 시작...\n")
    
    try:
        doc = fitz.open(str(pdf_path))
        total_pages = len(doc)
        
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
                        import base64
                        img_base64 = base64.b64encode(image_bytes).decode('utf-8')
                        images.append({
                            'index': img_index,
                            'base64': img_base64,
                            'ext': image_ext,
                            'width': base_image.get('width', 0),
                            'height': base_image.get('height', 0)
                        })
                    except Exception as e:
                        continue
            except Exception as e:
                pass
            return images
        
        def process_single_page_test(page_num, page):
            """단일 PDF 페이지 처리: 텍스트 추출 + 이미지 분석"""
            try:
                # 1. PyMuPDF로 텍스트 추출 (정확하고 빠름)
                page_text = page.get_text("text").strip()
                
                # 2. 페이지에서 이미지 추출
                page_images = extract_images_from_page(page, doc)
                
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
                print(f"페이지 {page_num + 1} 처리 중 오류 발생: {e}")
                import traceback
                traceback.print_exc()
                return (page_num + 1, "")
        
        pdf_texts = []
        
        batch_size = settings.OLLAMA_BATCH_SIZE
        print(f"총 {total_pages}페이지를 배치 크기 {batch_size}로 처리합니다...")
        print("(텍스트는 즉시 추출, 이미지가 있는 페이지만 Ollama로 분석)\n")
        
        # 배치 단위로 처리
        for batch_start in tqdm(range(0, total_pages, batch_size), desc="PDF 페이지 배치 처리"):
            batch_end = min(batch_start + batch_size, total_pages)
            batch_pages = list(range(batch_start, batch_end))
            
            # 병렬 처리로 배치 내 페이지들 처리
            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = {
                    executor.submit(process_single_page_test, page_num, doc[page_num]): page_num
                    for page_num in batch_pages
                }
                
                # 완료된 작업부터 결과 수집
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        pdf_texts.append(result)  # 빈 텍스트도 포함
                    except Exception as e:
                        page_num = futures[future]
                        print(f"페이지 {page_num + 1} 처리 실패: {e}")
                        pdf_texts.append((page_num + 1, ""))
        
        doc.close()
        
        # 페이지 번호 순으로 정렬
        pdf_texts.sort(key=lambda x: x[0])
        
        print(f"PDF parsing complete. {len(pdf_texts)} pages processed.\n")
    except Exception as e:
        print(f"❌ PDF 처리 실패: {e}")
        import traceback
        traceback.print_exc()
        return
    
    if not pdf_texts:
        print("⚠️  추출된 텍스트가 없습니다.")
        return
    
    # 결과 출력
    print("\n" + "=" * 80)
    print("📊 처리 결과 요약")
    print("=" * 80)
    print(f"총 페이지 수: {len(pdf_texts)}")
    print(f"텍스트가 있는 페이지: {sum(1 for _, text in pdf_texts if text.strip())}")
    print()
    
    # 각 페이지별 결과 출력
    output_dir = BASE_DIR / 'test_pdf_processing' / 'output'
    output_dir.mkdir(exist_ok=True)
    
    output_file = output_dir / '26_lecture_extracted_bakllava.txt'
    json_file = output_dir / '26_lecture_extracted_bakllava.json'
    
    print("=" * 80)
    print("📝 페이지별 추출 결과")
    print("=" * 80)
    
    results = []
    with open(output_file, 'w', encoding='utf-8') as f:
        for page_num, text in pdf_texts:
            page_info = {
                'page_num': page_num,
                'text': text,
                'text_length': len(text),
                'has_text': bool(text.strip())
            }
            results.append(page_info)
            
            print(f"\n[페이지 {page_num}]")
            print("-" * 80)
            if text.strip():
                print(f"텍스트 길이: {len(text)}자")
                print(f"\n전체 내용:")
                print("-" * 80)
                print(text)  # 전체 내용 출력
                print("-" * 80)
                
                # 파일에 저장
                f.write(f"\n{'='*80}\n")
                f.write(f"페이지 {page_num}\n")
                f.write(f"텍스트 길이: {len(text)}자\n")
                f.write(f"{'='*80}\n\n")
                f.write(text)
                f.write(f"\n\n")
            else:
                print("⚠️  텍스트가 추출되지 않았습니다.")
                f.write(f"\n{'='*80}\n")
                f.write(f"페이지 {page_num} - 텍스트 없음\n")
                f.write(f"{'='*80}\n\n")
    
    # JSON 형식으로도 저장
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump({
            'pdf_path': str(pdf_path),
            'model': 'bakllava',
            'total_pages': len(pdf_texts),
            'pages': results
        }, f, ensure_ascii=False, indent=2)
    
    print("\n" + "=" * 80)
    print("💾 결과 저장 완료")
    print("=" * 80)
    print(f"📄 텍스트 파일: {output_file}")
    print(f"📋 JSON 파일: {json_file}")
    print()
    
    # 통계 정보
    total_chars = sum(len(text) for _, text in pdf_texts)
    avg_chars_per_page = total_chars / len(pdf_texts) if pdf_texts else 0
    
    print("=" * 80)
    print("📈 통계 정보")
    print("=" * 80)
    print(f"총 문자 수: {total_chars:,}자")
    print(f"페이지당 평균 문자 수: {avg_chars_per_page:,.0f}자")
    print(f"텍스트가 있는 페이지 비율: {sum(1 for _, text in pdf_texts if text.strip()) / len(pdf_texts) * 100:.1f}%")
    print("=" * 80)

if __name__ == '__main__':
    main()

