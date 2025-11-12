from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _

# Streamlit의 config.py에 있던 경로 로직을 Django 모델로 가져옵니다.
# Django의 upload_to 함수는 MEDIA_ROOT를 포함하지 않은 상대 경로만 반환해야 합니다.
def audio_upload_path(instance, filename):
    return f"{instance.user.id}/{instance.lecture_name}_audio.{filename.split('.')[-1]}"

def pdf_upload_path(instance, filename):
    return f"{instance.user.id}/{instance.lecture_name}_lecture.pdf"

class CustomUser(AbstractUser):
    """
    커스텀 User 모델
    Django의 AbstractUser를 상속받아 기본 User 기능을 모두 포함합니다.
    
    현재 서비스에서 실사용 중인 주요 컬럼:
    - id: INTEGER (PK, 자동 생성)
    - username: VARCHAR(150), UNIQUE (로그인 아이디)
    - email: VARCHAR(254) (회원가입에서 입력받는 이메일)
    - password: VARCHAR(128) (해시된 비밀번호)
    - is_active: BOOLEAN (계정 활성화 여부, 인증 로직에서 사용)
    - is_staff: BOOLEAN (관리자 접근 권한 플래그, 관리자 페이지 접근 권한)
    - last_login: DATETIME (Django 인증 프레임워크가 로그인 시 자동 갱신)
    - date_joined: DATETIME (가입 일시)
    """
    
    class Meta:
        verbose_name = _('사용자')
        verbose_name_plural = _('사용자들')
        db_table = 'auth_user'  # 기존 User 테이블과 동일한 이름 사용

class Lecture(models.Model):
    """
    강의 모델
    사용자가 업로드한 강의 음성 파일과 PDF 파일을 저장하고 처리 상태를 관리합니다.
    YouTube URL을 통한 음성 다운로드도 지원합니다.
    
    컬럼:
    - id: INTEGER (PK, 자동 생성)
    - user_id: INTEGER (FK, CustomUser 참조) - 강의를 업로드한 사용자
    - lecture_name: VARCHAR(255) - 강의 이름
    - audio_file: VARCHAR(100) - 음성 파일 경로 (MEDIA_ROOT 기준, NULL 허용)
    - pdf_file: VARCHAR(100) - PDF 파일 경로 (MEDIA_ROOT 기준)
    - youtube_url: VARCHAR(500) - YouTube URL (NULL 허용, 파일 업로드 대신 사용 가능)
    - full_script: TEXT (NULL 허용) - STT 처리된 전체 스크립트 (타임스탬프 포함)
    - summary_json: JSON (NULL 허용) - Gemini로 생성된 요약 JSON 데이터
    - status: VARCHAR(20) - 처리 상태 ('processing': 처리 중, 'completed': 완료, 'failed': 실패)
    - current_step: INTEGER - 현재 처리 단계 (0~5)
    - estimated_time_sec: INTEGER - 예상 소요 시간(초). 업로드 시 오디오 길이와 PDF 페이지 수를 기반으로 계산되며, 
      ProcessingStats의 평균값을 사용하여 예측합니다. 초기값은 0이며, calculate_etr_task에서 비동기로 계산되어 업데이트됩니다.
    - created_at: DATETIME - 강의 생성 일시 (자동 생성)
    """
    # '처리중', '완료', '실패' 상태를 추적
    STATUS_CHOICES = [
        ('processing', '처리 중'),
        ('completed', '완료'),
        ('failed', '실패'),
    ]
    
    user = models.ForeignKey('CustomUser', on_delete=models.CASCADE, related_name='lectures', verbose_name="사용자")
    lecture_name = models.CharField(max_length=255, verbose_name="강의 이름")
    
    # FileField는 파일 자체를 'MEDIA_ROOT'에 저장합니다.
    audio_file = models.FileField(upload_to=audio_upload_path, verbose_name="음성 파일", blank=True, null=True)
    pdf_file = models.FileField(upload_to=pdf_upload_path, verbose_name="PDF 파일")
    youtube_url = models.URLField(max_length=500, blank=True, null=True, verbose_name="YouTube URL")
    
    full_script = models.TextField(blank=True, null=True, verbose_name="전체 스크립트")
    summary_json = models.JSONField(blank=True, null=True, verbose_name="요약 JSON")
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='processing')
    current_step = models.IntegerField(default=0, verbose_name="현재 진행 단계")
    # 예상 소요 시간(초): 오디오 길이와 PDF 페이지 수를 기반으로 ProcessingStats의 평균값을 사용하여 계산
    # 업로드 시 calculate_etr_task에서 비동기로 계산되어 업데이트됨
    estimated_time_sec = models.IntegerField(default=0, verbose_name="예상 소요 시간(초)")
    # 단계별 소요 시간(초) - JSON 형식: {"1": 10.5, "2": 25.3, ...}
    step_times = models.JSONField(default=dict, blank=True, verbose_name="단계별 소요 시간")
    # YouTube 다운로드 ETA(초) - YouTube URL을 사용하는 경우 다운로드 예상 소요 시간
    youtube_download_eta_sec = models.IntegerField(default=0, verbose_name="YouTube 다운로드 ETA(초)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['user', 'lecture_name']  # 같은 사용자 내에서 강의 이름은 고유해야 함
    
    def __str__(self):
        return f"{self.user.username} - {self.lecture_name}"

class PdfChunk(models.Model):
    """
    PDF 청크 모델
    PDF 파일을 페이지 단위로 분할하여 저장한 데이터입니다.
    
    컬럼:
    - id: INTEGER (PK, 자동 생성)
    - lecture_id: INTEGER (FK, Lecture 참조) - 해당 강의
    - page_num: INTEGER - PDF 페이지 번호 (1부터 시작)
    - content: TEXT - 해당 페이지의 텍스트 내용
    """
    lecture = models.ForeignKey(Lecture, related_name='chunks', on_delete=models.CASCADE)
    page_num = models.IntegerField()
    content = models.TextField()

    def __str__(self):
        return f"{self.lecture.lecture_name} - Page {self.page_num}"

class Mapping(models.Model):
    """
    매핑 모델
    강의 요약의 각 주제를 PDF의 해당 페이지와 의미 기반으로 매핑한 데이터입니다.
    
    컬럼:
    - id: INTEGER (PK, 자동 생성)
    - lecture_id: INTEGER (FK, Lecture 참조) - 해당 강의
    - summary_topic: VARCHAR(500) - 요약에서 추출된 주제/토픽
    - mapped_pdf_page: INTEGER - 매핑된 PDF 페이지 번호
    - mapped_pdf_content: TEXT - 매핑된 PDF 페이지의 실제 텍스트 내용
    """
    lecture = models.ForeignKey(Lecture, related_name='mappings', on_delete=models.CASCADE)
    summary_topic = models.CharField(max_length=500)
    mapped_pdf_page = models.IntegerField()
    mapped_pdf_content = models.TextField()

class ProcessingStats(models.Model):
    """
    처리 통계 모델
    각 단계별 평균 처리 속도를 영구적으로 저장하고 업데이트합니다.
    이 모델은 싱글톤 패턴으로 사용되며(pk=1), 모든 강의 처리 작업이 완료될 때마다 
    이동 평균 방식(기존 50%, 새 50%)으로 평균값을 업데이트합니다.
    
    컬럼:
    - id: INTEGER (PK, 자동 생성) - 항상 1로 고정 (싱글톤)
    - audio_stt_avg_sec_per_min: FLOAT - 1분의 오디오를 STT 처리하는 데 걸리는 평균 시간(초)
      예: 2.0이면 1분 오디오 처리에 평균 2초 소요
    - pdf_parsing_avg_sec_per_page: FLOAT - 1페이지의 PDF를 파싱하는 데 걸리는 평균 시간(초)
      예: 1.6이면 1페이지 PDF 파싱에 평균 1.6초 소요
    - embedding_avg_sec_per_page: FLOAT - 1페이지의 PDF를 임베딩하는 데 걸리는 평균 시간(초)
      예: 0.07이면 1페이지 PDF 임베딩에 평균 0.07초 소요
    - summary_avg_sec_per_min: FLOAT - 1분의 오디오를 요약하는 데 걸리는 평균 시간(초)
      예: 1.0이면 1분 오디오 요약에 평균 1초 소요
    - updated_at: DATETIME - 마지막 업데이트 일시 (자동 업데이트)
    
    사용 예시:
    - ETR 계산 (병렬 처리 구조):
      * 병렬 그룹 1: max(오디오_길이_분 * audio_stt_avg_sec_per_min, PDF_페이지_수 * pdf_parsing_avg_sec_per_page)
      * 병렬 그룹 2: max(오디오_길이_분 * summary_avg_sec_per_min, PDF_페이지_수 * embedding_avg_sec_per_page)
      * 순차 처리: 매핑 시간 (고정값 또는 추정)
      * 총 예상 시간 = 그룹1 + 그룹2 + 순차 처리
    """
    # 1분의 오디오를 STT 처리하는 데 걸리는 평균 시간(초)
    # process_lecture_task 완료 시 이동 평균 방식으로 업데이트됨
    audio_stt_avg_sec_per_min = models.FloatField(default=2.0, verbose_name="오디오 STT 평균(초/분)")
    
    # 1페이지의 PDF를 파싱하는 데 걸리는 평균 시간(초)
    # process_lecture_task 완료 시 이동 평균 방식으로 업데이트됨
    pdf_parsing_avg_sec_per_page = models.FloatField(default=1.6, verbose_name="PDF 파싱 평균(초/페이지)")
    
    # 1페이지의 PDF를 임베딩하는 데 걸리는 평균 시간(초)
    # process_lecture_task 완료 시 이동 평균 방식으로 업데이트됨
    embedding_avg_sec_per_page = models.FloatField(default=0.07, verbose_name="임베딩 평균(초/페이지)")
    
    # 1분의 오디오를 요약하는 데 걸리는 평균 시간(초)
    # process_lecture_task 완료 시 이동 평균 방식으로 업데이트됨
    summary_avg_sec_per_min = models.FloatField(default=1.0, verbose_name="요약 평균(초/분)")
    
    updated_at = models.DateTimeField(auto_now=True, verbose_name="업데이트 일시")
    
    class Meta:
        verbose_name = _('처리 통계')
        verbose_name_plural = _('처리 통계')
    
    def __str__(self):
        return f"ProcessingStats (updated: {self.updated_at})"
    
    @classmethod
    def get_or_create_singleton(cls):
        """
        싱글톤 인스턴스를 가져오거나 생성합니다.
        
        이 메서드는 pk=1인 ProcessingStats 인스턴스를 반환합니다.
        존재하지 않으면 기본값으로 새로 생성합니다.
        
        Returns:
            ProcessingStats: pk=1인 싱글톤 인스턴스
        """
        obj, created = cls.objects.get_or_create(pk=1)
        return obj