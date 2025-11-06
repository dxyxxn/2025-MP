import os
from celery import Celery

# Django의 settings.py 파일을 Celery 설정으로 사용합니다.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# 'config'는 이 Django 프로젝트의 이름입니다.
app = Celery('config')

# 'CELERY_'라는 접두사를 가진 모든 Django 설정을 로드합니다.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Django 앱 설정에 등록된 모든 tasks.py 파일을 자동으로 로드합니다.
app.autodiscover_tasks()