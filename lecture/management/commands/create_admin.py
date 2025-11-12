"""
admin 계정을 생성하는 관리 명령어

사용법:
    python manage.py create_admin
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = 'admin 계정을 생성합니다 (아이디: admin, 비밀번호: 000000)'

    def handle(self, *args, **options):
        User = get_user_model()
        
        # admin 계정이 이미 존재하는지 확인
        if User.objects.filter(username='admin').exists():
            self.stdout.write(
                self.style.WARNING('admin 계정이 이미 존재합니다.')
            )
            # 기존 계정에 is_staff 플래그 설정
            admin_user = User.objects.get(username='admin')
            admin_user.is_staff = True
            admin_user.set_password('000000')
            admin_user.save()
            self.stdout.write(
                self.style.SUCCESS('기존 admin 계정의 비밀번호를 업데이트하고 is_staff 플래그를 설정했습니다.')
            )
        else:
            # 새 admin 계정 생성
            admin_user = User.objects.create_user(
                username='admin',
                password='000000',
                is_staff=True,
                is_active=True
            )
            self.stdout.write(
                self.style.SUCCESS(f'admin 계정이 성공적으로 생성되었습니다. (아이디: admin, 비밀번호: 000000)')
            )

