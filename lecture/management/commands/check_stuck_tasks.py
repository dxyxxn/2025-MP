"""
오래된 '처리 중' 상태의 강의를 감지하고 실패로 표시하는 관리 명령어

사용법:
    python manage.py check_stuck_tasks [--minutes MINUTES] [--dry-run]

옵션:
    --minutes: 몇 분 이상 지난 작업을 실패로 표시할지 지정 (기본값: 15)
    --dry-run: 실제로 상태를 변경하지 않고 어떤 작업이 영향을 받을지만 표시
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from lecture.models import Lecture


class Command(BaseCommand):
    help = '오래된 "처리 중" 상태의 강의를 감지하고 실패로 표시합니다.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--minutes',
            type=int,
            default=15,
            help='몇 분 이상 지난 작업을 실패로 표시할지 지정 (기본값: 15)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='실제로 상태를 변경하지 않고 어떤 작업이 영향을 받을지만 표시',
        )

    def handle(self, *args, **options):
        minutes = options['minutes']
        dry_run = options['dry_run']
        
        # 현재 시간에서 지정된 시간을 뺀 시점 이전에 생성된 작업 찾기
        cutoff_time = timezone.now() - timedelta(minutes=minutes)
        
        # '처리 중' 상태이고 지정된 시간 이상 지난 강의 찾기
        stuck_lectures = Lecture.objects.filter(
            status='processing',
            created_at__lt=cutoff_time
        ).order_by('created_at')
        
        count = stuck_lectures.count()
        
        if count == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f'오래된 "처리 중" 상태의 강의가 없습니다. (기준: {minutes}분 이상)'
                )
            )
            return
        
        self.stdout.write(
            self.style.WARNING(
                f'{count}개의 오래된 "처리 중" 상태 강의를 발견했습니다.'
            )
        )
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING('--dry-run 모드: 실제로 상태를 변경하지 않습니다.')
            )
        
        # 각 강의에 대해 처리
        updated_count = 0
        for lecture in stuck_lectures:
            age_minutes = (timezone.now() - lecture.created_at).total_seconds() / 60
            self.stdout.write(
                f'  - 강의 ID {lecture.id}: "{lecture.lecture_name}" '
                f'(생성: {lecture.created_at.strftime("%Y-%m-%d %H:%M:%S")}, '
                f'경과: {age_minutes:.1f}분)'
            )
            
            if not dry_run:
                lecture.status = 'failed'
                lecture.save()
                updated_count += 1
        
        if not dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f'\n{updated_count}개의 강의 상태를 "실패"로 업데이트했습니다.'
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f'\n--dry-run 모드: {count}개의 강의가 업데이트될 것입니다.'
                )
            )

