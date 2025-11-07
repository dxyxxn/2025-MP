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
    
    컬럼:
    - id: INTEGER (PK, 자동 생성)
    - username: VARCHAR(150), UNIQUE (로그인 아이디)
    - password: VARCHAR(128) (해시된 비밀번호)
    - email: VARCHAR(254)
    - first_name: VARCHAR(150)
    - last_name: VARCHAR(150)
    - is_staff: BOOLEAN (관리자 페이지 접근 여부)
    - is_active: BOOLEAN (활성 계정 여부)
    - date_joined: DATETIME (가입 일시)
    """
    # AbstractUser가 이미 모든 필드를 포함하므로 추가 필드 정의 불필요
    # 필요시 여기에 추가 필드를 정의할 수 있습니다.
    
    class Meta:
        verbose_name = _('사용자')
        verbose_name_plural = _('사용자들')
        db_table = 'auth_user'  # 기존 User 테이블과 동일한 이름 사용

class Lecture(models.Model):
    # '처리중', '완료', '실패' 상태를 추적
    STATUS_CHOICES = [
        ('processing', '처리 중'),
        ('completed', '완료'),
        ('failed', '실패'),
    ]
    
    user = models.ForeignKey('CustomUser', on_delete=models.CASCADE, related_name='lectures', verbose_name="사용자")
    lecture_name = models.CharField(max_length=255, verbose_name="강의 이름")
    
    # FileField는 파일 자체를 'MEDIA_ROOT'에 저장합니다.
    audio_file = models.FileField(upload_to=audio_upload_path, verbose_name="음성 파일")
    pdf_file = models.FileField(upload_to=pdf_upload_path, verbose_name="PDF 파일")
    
    full_script = models.TextField(blank=True, null=True, verbose_name="전체 스크립트")
    summary_json = models.JSONField(blank=True, null=True, verbose_name="요약 JSON")
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='processing')
    current_step = models.IntegerField(default=0, verbose_name="현재 진행 단계")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['user', 'lecture_name']  # 같은 사용자 내에서 강의 이름은 고유해야 함
    
    def __str__(self):
        return f"{self.user.username} - {self.lecture_name}"

class PdfChunk(models.Model):
    lecture = models.ForeignKey(Lecture, related_name='chunks', on_delete=models.CASCADE)
    page_num = models.IntegerField()
    content = models.TextField()

    def __str__(self):
        return f"{self.lecture.lecture_name} - Page {self.page_num}"

class Mapping(models.Model):
    lecture = models.ForeignKey(Lecture, related_name='mappings', on_delete=models.CASCADE)
    summary_topic = models.CharField(max_length=500)
    mapped_pdf_page = models.IntegerField()
    mapped_pdf_content = models.TextField()