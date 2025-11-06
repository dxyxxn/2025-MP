from django.db import models

# Streamlit의 config.py에 있던 경로 로직을 Django 모델로 가져옵니다.
# Django의 upload_to 함수는 MEDIA_ROOT를 포함하지 않은 상대 경로만 반환해야 합니다.
def audio_upload_path(instance, filename):
    return f"{instance.lecture_name}_audio.{filename.split('.')[-1]}"

def pdf_upload_path(instance, filename):
    return f"{instance.lecture_name}_lecture.pdf"

class Lecture(models.Model):
    # '처리중', '완료', '실패' 상태를 추적
    STATUS_CHOICES = [
        ('processing', '처리 중'),
        ('completed', '완료'),
        ('failed', '실패'),
    ]
    
    lecture_name = models.CharField(max_length=255, unique=True, verbose_name="강의 이름")
    
    # FileField는 파일 자체를 'MEDIA_ROOT'에 저장합니다.
    audio_file = models.FileField(upload_to=audio_upload_path, verbose_name="음성 파일")
    pdf_file = models.FileField(upload_to=pdf_upload_path, verbose_name="PDF 파일")
    
    full_script = models.TextField(blank=True, null=True, verbose_name="전체 스크립트")
    summary_json = models.JSONField(blank=True, null=True, verbose_name="요약 JSON")
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='processing')
    current_step = models.IntegerField(default=0, verbose_name="현재 진행 단계")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.lecture_name

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