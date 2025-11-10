# PDF 처리 테스트

Ollama 멀티모달 모델(bakllava)을 사용한 PDF 처리 결과를 테스트하는 스크립트입니다.

## 사용 방법

```bash
# 프로젝트 루트 디렉토리에서 실행
cd /home/rlaehdbs1805/2025MP_2
python test_pdf_processing/test_bakllava_pdf.py
```

## 입력 파일

- `media_uploads/26_lecture.pdf`

## 출력 파일

실행 후 `test_pdf_processing/output/` 디렉토리에 다음 파일들이 생성됩니다:

- `26_lecture_extracted_bakllava.txt`: 각 페이지별 추출된 텍스트 (읽기 쉬운 형식)
- `26_lecture_extracted_bakllava.json`: 각 페이지별 추출된 텍스트 (JSON 형식)

## 처리 방식

1. **PyMuPDF로 텍스트 추출**: 페이지의 텍스트를 정확하고 빠르게 추출
2. **이미지 객체 추출**: 페이지에 포함된 이미지들을 추출
3. **Ollama 이미지 분석**: 각 이미지를 Ollama bakllava 모델로 분석 (영어)
4. **결합**: 텍스트 + 이미지 설명을 결합하여 최종 결과 생성

## 출력 내용

- 각 페이지별 추출된 텍스트 (전체 내용)
- 이미지 설명 (있는 경우)
- 페이지별 통계 정보 (텍스트 길이, 유무 등)
- 전체 통계 정보 (총 문자 수, 평균 문자 수 등)

