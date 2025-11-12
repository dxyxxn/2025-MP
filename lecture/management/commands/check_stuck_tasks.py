"""
오래된 '처리 중' 상태의 강의를 감지하고 실패로 표시하는 관리 명령어

사용법:
    python manage.py check_stuck_tasks [--minutes MINUTES] [--dry-run]

옵션:
    --minutes: 몇 분 이상 지난 작업을 실패로 표시할지 지정 (기본값: 18)
    --dry-run: 실제로 상태를 변경하지 않고 어떤 작업이 영향을 받을지만 표시
"""
from django.core.management.base import BaseCommand
from lecture.tasks import check_and_mark_stuck_tasks


class Command(BaseCommand):
    help = '오래된 "처리 중" 상태의 강의를 감지하고 실패로 표시합니다.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--minutes',
            type=int,
            default=18,
            help='몇 분 이상 지난 작업을 실패로 표시할지 지정 (기본값: 18)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='실제로 상태를 변경하지 않고 어떤 작업이 영향을 받을지만 표시',
        )

    def handle(self, *args, **options):
        minutes = options['minutes']
        dry_run = options['dry_run']
        
        # tasks.py의 공유 함수 사용
        count, updated_count = check_and_mark_stuck_tasks(minutes=minutes, dry_run=dry_run)
        
        # 사용자 친화적인 출력 (관리 명령어용)
        if count == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f'오래된 "처리 중" 상태의 강의가 없습니다. (기준: {minutes}분 이상)'
                )
            )
        else:
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

