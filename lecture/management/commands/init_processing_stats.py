"""
ProcessingStats 데이터베이스를 기본값으로 초기화하는 관리 명령어

사용법:
    python manage.py init_processing_stats
"""
from django.core.management.base import BaseCommand
from lecture.models import ProcessingStats


class Command(BaseCommand):
    help = 'ProcessingStats 데이터베이스를 기본값으로 초기화합니다'

    def handle(self, *args, **options):
        # 싱글톤 인스턴스 가져오기 또는 생성
        stats, created = ProcessingStats.objects.get_or_create(pk=1)
        
        # 기본값 설정
        stats.audio_stt_avg_sec_per_min = 1.8  # STT: 1.8초/분
        stats.summary_avg_sec_per_min = 1.8  # 요약: 1.8초/분
        stats.embedding_avg_sec_per_page = 0.08  # 임베딩: 0.08초/페이지
        stats.pdf_parsing_avg_sec_per_page = 1.25  # PDF: 1.25초/페이지
        
        stats.save()
        
        if created:
            self.stdout.write(
                self.style.SUCCESS('ProcessingStats가 기본값으로 생성되었습니다.')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS('ProcessingStats가 기본값으로 초기화되었습니다.')
            )
        
        self.stdout.write(
            self.style.SUCCESS(
                f'\n초기화된 값:\n'
                f'  - STT 평균: {stats.audio_stt_avg_sec_per_min}초/분\n'
                f'  - 요약 평균: {stats.summary_avg_sec_per_min}초/분\n'
                f'  - 임베딩 평균: {stats.embedding_avg_sec_per_page}초/페이지\n'
                f'  - PDF 파싱 평균: {stats.pdf_parsing_avg_sec_per_page}초/페이지'
            )
        )

