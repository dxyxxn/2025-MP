from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import login, authenticate, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, connection, models
from django.conf import settings
from django.contrib import messages
from django.apps import apps
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
import os
from urllib.parse import quote
from .models import Lecture, ProcessingStats, CustomUser, PdfChunk, Mapping
from .tasks import process_lecture_task, calculate_etr_task, start_process_from_url_task # Celery 태스크 임포트
from .services import init_gemini_models, init_chromadb_client, get_rag_response
import json
import re
import markdown
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor

# 한글 폰트 등록 함수
def register_korean_font():
    """한글 폰트를 찾아서 등록합니다."""
    korean_font_name = 'KoreanFont'
    korean_font_bold_name = 'KoreanFont-Bold'
    
    # 이미 등록되어 있으면 스킵
    if korean_font_name in pdfmetrics.getRegisteredFontNames():
        return korean_font_name
    
    # 가능한 폰트 경로 목록
    font_paths = [
        # Noto Sans CJK
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
        # Nanum 폰트
        '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
        '/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf',
        # Windows 폰트 (WSL) - 대소문자 구분
        '/mnt/c/Windows/Fonts/malgun.ttf',  # 맑은 고딕
        '/mnt/c/Windows/Fonts/MALGUN.TTF',  # 맑은 고딕 (대문자)
        '/mnt/c/Windows/Fonts/gulim.ttc',    # 굴림
        '/mnt/c/Windows/Fonts/GULIM.TTC',    # 굴림 (대문자)
        '/mnt/c/Windows/Fonts/batang.ttc',  # 바탕
        '/mnt/c/Windows/Fonts/BATANG.TTC',  # 바탕 (대문자)
        # 사용자 폰트 디렉토리
        os.path.expanduser('~/.fonts/NanumGothic.ttf'),
        os.path.expanduser('~/.local/share/fonts/NanumGothic.ttf'),
    ]
    
    # 볼드 폰트 경로 목록
    bold_font_paths = [
        '/mnt/c/Windows/Fonts/malgunbd.ttf',  # 맑은 고딕 볼드
        '/mnt/c/Windows/Fonts/MALGUNBD.TTF',
        '/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf',
        '/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf',
    ]
    
    # 폰트 찾기
    font_path = None
    for path in font_paths:
        if os.path.exists(path):
            font_path = path
            break
    
    if font_path:
        try:
            # TTC 파일인 경우 (Noto Sans CJK)
            if font_path.endswith('.ttc'):
                # TTC 파일은 여러 폰트를 포함하므로 첫 번째 폰트 사용
                pdfmetrics.registerFont(TTFont(korean_font_name, font_path, subfontIndex=0))
            else:
                pdfmetrics.registerFont(TTFont(korean_font_name, font_path))
            
            # 볼드 폰트 찾기 및 등록
            bold_font_path = None
            for path in bold_font_paths:
                if os.path.exists(path):
                    bold_font_path = path
                    break
            
            if bold_font_path:
                try:
                    pdfmetrics.registerFont(TTFont(korean_font_bold_name, bold_font_path))
                    print(f"볼드 폰트 등록 성공: {bold_font_path}")
                except Exception as e:
                    print(f"볼드 폰트 등록 실패: {e}")
            
            return korean_font_name
        except Exception as e:
            print(f"폰트 등록 실패 ({font_path}): {e}")
    
    # 폰트를 찾지 못한 경우 기본 폰트 사용 (한글이 깨질 수 있음)
    print("경고: 한글 폰트를 찾을 수 없습니다. 한글이 제대로 표시되지 않을 수 있습니다.")
    print("해결 방법: sudo apt-get install fonts-noto-cjk 또는 sudo apt-get install fonts-nanum")
    return 'Helvetica'  # 기본 폰트

# 로그인 페이지
def login_view(request):
    if request.user.is_authenticated:
        # next 파라미터 확인
        next_url = request.GET.get('next') or request.POST.get('next')
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts=None):
            return redirect(next_url)
        # is_staff이면 관리자 페이지로, 아니면 upload 페이지로
        if request.user.is_staff:
            return redirect('admin_dashboard')
        return redirect('upload')
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        next_url = request.POST.get('next') or request.GET.get('next')
        
        if username and password:
            user = authenticate(request, username=username, password=password)
            if user is not None:
                login(request, user)
                # next 파라미터가 있고 안전한 URL이면 해당 페이지로 리다이렉트
                if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts=None):
                    return redirect(next_url)
                # is_staff이면 관리자 페이지로, 아니면 upload 페이지로
                if user.is_staff:
                    return redirect('admin_dashboard')
                return redirect('upload')
            else:
                messages.error(request, '아이디 또는 비밀번호가 올바르지 않습니다.')
        else:
            messages.error(request, '아이디와 비밀번호를 모두 입력해주세요.')
    
    # GET 요청 시 next 파라미터를 템플릿에 전달
    context = {'next': request.GET.get('next', '')}
    return render(request, 'lecture/login.html', context)

# 회원가입 페이지
def signup_view(request):
    if request.user.is_authenticated:
        return redirect('upload')
    
    User = get_user_model()
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        password_confirm = request.POST.get('password_confirm')
        email = request.POST.get('email', '')
        
        if not username or not password:
            messages.error(request, '아이디와 비밀번호를 모두 입력해주세요.')
        elif password != password_confirm:
            messages.error(request, '비밀번호가 일치하지 않습니다.')
        elif User.objects.filter(username=username).exists():
            messages.error(request, '이미 존재하는 아이디입니다.')
        else:
            try:
                user = User.objects.create_user(
                    username=username,
                    password=password,
                    email=email
                )
                login(request, user)
                messages.success(request, '회원가입이 완료되었습니다.')
                return redirect('upload')
            except Exception as e:
                messages.error(request, f'회원가입 중 오류가 발생했습니다: {str(e)}')
    
    return render(request, 'lecture/signup.html')

# 로그아웃
def logout_view(request):
    logout(request)
    messages.success(request, '로그아웃되었습니다.')
    return redirect('login')

# 1. 업로드 페이지
@login_required
def upload_view(request):
    if request.method == 'POST':
        lecture_name = request.POST.get('lecture_name', '').strip()
        audio_input_type = request.POST.get('audio_input_type', 'file')
        audio_file = request.FILES.get('audio_file')
        youtube_url = request.POST.get('youtube_url', '').strip()
        pdf_file = request.FILES.get('pdf_file')
        
        # 빈 문자열 체크
        if not lecture_name:
            error_message = "강의 이름을 입력해주세요."
            lectures = Lecture.objects.filter(user=request.user).order_by('-created_at')
            return render(request, 'lecture/upload.html', {
                'lectures': lectures,
                'error_message': error_message
            })
        
        # 입력 방식 검증
        if audio_input_type == 'file':
            if not audio_file:
                error_message = "음성 파일을 선택해주세요."
                lectures = Lecture.objects.filter(user=request.user).order_by('-created_at')
                return render(request, 'lecture/upload.html', {
                    'lectures': lectures,
                    'error_message': error_message
                })
        elif audio_input_type == 'url':
            if not youtube_url:
                error_message = "YouTube URL을 입력해주세요."
                lectures = Lecture.objects.filter(user=request.user).order_by('-created_at')
                return render(request, 'lecture/upload.html', {
                    'lectures': lectures,
                    'error_message': error_message
                })
        else:
            error_message = "올바른 입력 방식을 선택해주세요."
            lectures = Lecture.objects.filter(user=request.user).order_by('-created_at')
            return render(request, 'lecture/upload.html', {
                'lectures': lectures,
                'error_message': error_message
            })
        
        # 강의 이름 중복 체크 (같은 사용자 내에서만)
        existing_lectures = Lecture.objects.filter(user=request.user)
        for existing in existing_lectures:
            existing_name_normalized = existing.lecture_name.strip() if existing.lecture_name else ""
            input_name_normalized = lecture_name.strip() if lecture_name else ""
            # 빈 문자열이 아닌 경우에만 비교
            if existing_name_normalized and input_name_normalized and existing_name_normalized == input_name_normalized:
                error_message = f"강의 이름 '{lecture_name}'은(는) 이미 존재합니다. 다른 이름을 사용해주세요."
                lectures = Lecture.objects.filter(user=request.user).order_by('-created_at')
                return render(request, 'lecture/upload.html', {
                    'lectures': lectures,
                    'error_message': error_message
                })

        # DB에 파일과 '처리중' 상태 저장
        lecture = None
        try:
            # 1. DB에 파일과 '처리중' 상태 저장
            if audio_input_type == 'file':
                # 파일 업로드 방식
                lecture = Lecture.objects.create(
                    user=request.user,
                    lecture_name=lecture_name,
                    audio_file=audio_file,
                    pdf_file=pdf_file,
                    status='processing',
                    estimated_time_sec=0  # 초기값, 나중에 업데이트됨
                )
                
                # 2. Celery 태스크 호출 (백그라운드 실행)
                process_lecture_task.delay(lecture.id)
                
                # 3. ETR 계산 태스크 호출 (비동기, 빠른 계산)
                calculate_etr_task.delay(lecture.id)
            else:
                # YouTube URL 방식
                lecture = Lecture.objects.create(
                    user=request.user,
                    lecture_name=lecture_name,
                    pdf_file=pdf_file,
                    youtube_url=youtube_url,
                    status='processing',
                    estimated_time_sec=0  # 초기값, 나중에 업데이트됨
                )
                
                # 2. YouTube 다운로드 및 처리 태스크 호출 (백그라운드 실행)
                start_process_from_url_task.delay(lecture.id)
            
            # 4. 처리 중 페이지로 즉시 리다이렉트
            return redirect('lecture_detail', lecture_id=lecture.id)
        except IntegrityError as e:
            # 데이터베이스 레벨에서 중복 체크 (race condition 대비)
            # IntegrityError 발생 시 파일은 이미 저장되었을 수 있으므로 삭제 시도
            # 실제로 중복인지 확인
            error_str = str(e).lower()
            if 'unique' in error_str or 'duplicate' in error_str:
                # 실제 중복 오류인 경우
                try:
                    if lecture and hasattr(lecture, 'audio_file') and lecture.audio_file:
                        try:
                            if os.path.exists(lecture.audio_file.path):
                                os.remove(lecture.audio_file.path)
                        except (AttributeError, ValueError):
                            pass
                    if lecture and hasattr(lecture, 'pdf_file') and lecture.pdf_file:
                        try:
                            if os.path.exists(lecture.pdf_file.path):
                                os.remove(lecture.pdf_file.path)
                        except (AttributeError, ValueError):
                            pass
                except Exception:
                    # 파일 삭제 실패는 무시
                    pass
                
                error_message = f"강의 이름 '{lecture_name}'은(는) 이미 존재합니다. 다른 이름을 사용해주세요."
            else:
                # 다른 종류의 IntegrityError
                error_message = f"데이터베이스 오류가 발생했습니다: {str(e)}"
            
            lectures = Lecture.objects.filter(user=request.user).order_by('-created_at')
            return render(request, 'lecture/upload.html', {
                'lectures': lectures,
                'error_message': error_message
            })
        except Exception as e:
            # 기타 예외 처리
            error_message = f"오류가 발생했습니다: {str(e)}"
            lectures = Lecture.objects.filter(user=request.user).order_by('-created_at')
            return render(request, 'lecture/upload.html', {
                'lectures': lectures,
                'error_message': error_message
            })

    # GET 요청 시: 기존 강의 목록 표시 (현재 사용자의 강의만)
    lectures = Lecture.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'lecture/upload.html', {'lectures': lectures})

# 2. 메인 학습 페이지
@login_required
def lecture_detail_view(request, lecture_id):
    # 소유자 확인 제거: 로그인한 모든 사용자가 접근 가능
    lecture = get_object_or_404(Lecture, id=lecture_id)
    
    # 처리 중이면 다른 페이지 표시 (간소화)
    if lecture.status != 'completed':
        return render(request, 'lecture/processing.html', {'lecture': lecture})
        
    # --- [수정] 템플릿에 보낼 데이터 가공 ---
    # 1. JSON에서 '요약 리스트'를 가져옴
    summary_data = json.loads(lecture.summary_json) if lecture.summary_json else {}
    summary_list_from_json = summary_data.get('summary_list', [])
    
    # 2. DB에서 '매핑 정보'를 가져와 {주제: 페이지} 딕셔너리로 변환
    mappings_dict = {mapping.summary_topic: mapping.mapped_pdf_page for mapping in lecture.mappings.all()}
    
    # 3. '요약 리스트'에 '매핑된 페이지' 정보를 추가
    final_summary_list = []
    for item in summary_list_from_json:
        topic = item.get('topic')
        page_num = mappings_dict.get(topic) # 딕셔너리에서 주제로 페이지 번호 검색
        item['mapped_page'] = page_num # item 딕셔너리에 'mapped_page' 키 추가
        final_summary_list.append(item)
    # ----------------------------------------

    # 소유자 여부 확인
    is_owner = (lecture.user == request.user)

    context = {
        'lecture': lecture,
        'summary_list': summary_data.get('summary_list') if summary_data else [],
        'mappings': lecture.mappings.all(), # (이건 로직 수정 필요)
        'is_owner': is_owner  # 소유자 여부를 템플릿에 전달
    }
    return render(request, 'lecture/main.html', context)

# 3. RAG 챗봇 API (JavaScript와 통신)
@csrf_exempt # (데모용으로 CSRF 비활성화, 실제론 토큰 사용)
@login_required
def api_chat_view(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        lecture_id = data.get('lecture_id')
        query_text = data.get('query_text')
        
        try:
            # 강의 존재 확인
            lecture = get_object_or_404(Lecture, id=lecture_id)
            
            # 소유자 확인: 소유자가 아닌 경우 에러 반환
            if lecture.user != request.user:
                return JsonResponse({
                    'role': 'assistant', 
                    'content': '강의 소유자만 RAG 질의응답을 사용할 수 있습니다.'
                }, status=403)
            
            # 서비스 로직 호출
            models = init_gemini_models()
            chroma_client = init_chromadb_client()
            response_text = get_rag_response(lecture_id, query_text, models['flash'], models['embedding'], chroma_client)
            
            return JsonResponse({'role': 'assistant', 'content': response_text})
        except Exception as e:
            return JsonResponse({'role': 'assistant', 'content': str(e)}, status=500)

# 4. 상태 폴링 API (JavaScript와 통신)
@login_required
def api_lecture_status_view(request, lecture_id):
    lecture = get_object_or_404(Lecture, id=lecture_id, user=request.user)
    current_step = int(lecture.current_step) if lecture.current_step is not None else 0
    
    # 현재 단계의 소요 시간 가져오기
    step_time = None
    if lecture.step_times and str(current_step) in lecture.step_times:
        step_time = lecture.step_times[str(current_step)]
    
    return JsonResponse({
        'status': lecture.status, 
        'name': lecture.lecture_name,
        'current_step': current_step,
        'estimated_time_sec': lecture.estimated_time_sec,
        'step_time': step_time,  # 현재 단계의 소요 시간
        'youtube_url': lecture.youtube_url if lecture.youtube_url else None  # YouTube URL 여부 확인용
    })

# 5. 요약 파일 다운로드
@login_required
def download_summary_view(request, lecture_id):
    """
    강의의 소주제별 요약본을 Markdown 형식으로 출력한 후 PDF 파일로 다운로드합니다.
    """
    lecture = get_object_or_404(Lecture, id=lecture_id, user=request.user)
    
    # 요약 데이터가 없으면 에러 반환
    if not lecture.summary_json:
        messages.error(request, '요약 데이터가 없습니다.')
        return redirect('lecture_detail', lecture_id=lecture_id)
    
    try:
        # JSON 데이터 파싱
        summary_data = json.loads(lecture.summary_json) if isinstance(lecture.summary_json, str) else lecture.summary_json
        summary_list = summary_data.get('summary_list', [])
        
        # 매핑 정보 가져오기
        mappings_dict = {mapping.summary_topic: mapping.mapped_pdf_page for mapping in lecture.mappings.all()}
        
        # Markdown 형식으로 내용 생성
        md_content = []
        md_content.append(f"# {lecture.lecture_name}\n\n")
        # 서울 시간대로 변환
        seoul_time = timezone.localtime(lecture.created_at)
        md_content.append(f"**문서 생성일:** {seoul_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        md_content.append("---\n\n")
        
        for idx, item in enumerate(summary_list, 1):
            md_content.append(f"### 소주제 {idx}: {item.get('topic', '제목 없음')}\n\n")
            
            md_content.append(f"### 요약\n\n{item.get('summary', '요약 내용 없음')}\n\n")
                     
            if item.get('original_segment'):
                md_content.append(f"### 원본 구간\n\n")

                md_content.append(f"{item.get('original_segment')}\n\n")

                if item.get('timestamp'):
                    # 요약 다운로드에서는 타임스탬프를 일반 텍스트로 표시 (볼드/코드 형식 제거)
                    md_content.append(f" (출처 : 타임스탬프: {item.get('timestamp')} ")

                if item.get('topic') in mappings_dict:
                    md_content.append(f"PDF 페이지: {mappings_dict[item.get('topic')]}p)\n\n")

            md_content.append("---\n\n")
        
        # Markdown을 HTML로 변환
        markdown_text = ''.join(md_content)
        html_content = markdown.markdown(markdown_text, extensions=['extra'])
        
        # 디버깅용: HTML 출력 (서버 콘솔에서 확인)
        print("=== 생성된 HTML (처음 1000자) ===")
        print(html_content[:1000])
        print("=================================")
        
        # reportlab을 사용하여 PDF 생성
        pdf_buffer = BytesIO()
        doc = SimpleDocTemplate(pdf_buffer, pagesize=A4,
                                rightMargin=2*cm, leftMargin=2*cm,
                                topMargin=2*cm, bottomMargin=2*cm)
        
        # 한글 폰트 등록
        korean_font = register_korean_font()
        
        # 스타일 정의
        styles = getSampleStyleSheet()
        
        # 커스텀 스타일 정의 (한글 폰트 사용)
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=HexColor('#2c3e50'),
            spaceAfter=20,
            alignment=TA_LEFT,
            fontName=korean_font
        )
        
        heading2_style = ParagraphStyle(
            'CustomHeading2',
            parent=styles['Heading2'],
            fontSize=18,
            textColor=HexColor('#34495e'),
            spaceBefore=20,
            spaceAfter=12,
            fontName=korean_font
        )
        
        heading3_style = ParagraphStyle(
            'CustomHeading3',
            parent=styles['Heading3'],
            fontSize=14,
            textColor=HexColor('#7f8c8d'),
            spaceBefore=15,
            spaceAfter=10,
            fontName=korean_font
        )
        
        normal_style = ParagraphStyle(
            'CustomNormal',
            parent=styles['Normal'],
            fontSize=11,
            leading=16,
            textColor=HexColor('#333333'),
            spaceAfter=10,
            fontName=korean_font
        )
        
        # HTML을 reportlab 요소로 변환
        story = []
        
        # HTML을 reportlab이 지원하는 형식으로 간단하게 변환
        # BeautifulSoup 대신 정규식으로 간단하게 처리
        import re as re_module
        
        # HTML 태그를 제거하고 텍스트만 추출하거나, reportlab이 지원하는 태그만 남기기
        # 먼저 <strong> -> <b>, <em> -> <i> 변환 (중첩 태그도 처리)
        # 여러 번 반복하여 중첩된 경우도 처리
        max_iterations = 10
        for _ in range(max_iterations):
            old_content = html_content
            html_content = re_module.sub(r'<strong>(.*?)</strong>', r'<b>\1</b>', html_content, flags=re_module.DOTALL)
            html_content = re_module.sub(r'<em>(.*?)</em>', r'<i>\1</i>', html_content, flags=re_module.DOTALL)
            if old_content == html_content:
                break
        
        # <code> 태그 처리
        html_content = re_module.sub(r'<code>(.*?)</code>', r'<font face="Courier" color="#e74c3c"><b>\1</b></font>', html_content, flags=re_module.DOTALL)
        
        # <hr> 태그는 그대로 유지 (파서에서 처리)
        
        # HTML 파싱하여 요소 생성
        from html.parser import HTMLParser
        
        class HTMLToReportLab(HTMLParser):
            def __init__(self, story, styles):
                super().__init__()
                self.story = story
                self.styles = styles
                self.current_style = normal_style
                self.text_buffer = []
                self.in_paragraph = False
            
            def handle_starttag(self, tag, attrs):
                if tag == 'h1':
                    self.flush_text()
                    self.current_style = title_style
                    self.in_paragraph = True
                elif tag == 'h2':
                    self.flush_text()
                    self.current_style = heading2_style
                    self.in_paragraph = True
                elif tag == 'h3':
                    self.flush_text()
                    self.current_style = heading3_style
                    self.in_paragraph = True
                elif tag == 'p':
                    # <p> 태그 시작 시 이전 내용을 flush하고 스타일 설정
                    # 하지만 버퍼는 비우지 않음 (태그 안의 내용을 받기 위해)
                    if not self.in_paragraph:
                        self.flush_text()
                    self.current_style = normal_style
                    self.in_paragraph = True
                elif tag == 'hr':
                    self.flush_text()
                    # 구분선 추가 (더 눈에 띄게)
                    self.story.append(Spacer(1, 0.3*cm))
                    # 구분선을 위한 선 그리기
                    table = Table([['']], colWidths=[16*cm])
                    table.setStyle(TableStyle([
                        ('LINEBELOW', (0, 0), (-1, -1), 1, HexColor('#cccccc')),
                    ]))
                    self.story.append(table)
                    self.story.append(Spacer(1, 0.3*cm))
                elif tag == 'br':
                    self.text_buffer.append('<br/>')
                elif tag == 'strong' or tag == 'b':
                    # ReportLab의 Paragraph는 <b> 태그를 지원하지만, 한글 폰트에 볼드가 없을 수 있음
                    # 볼드 폰트가 등록되어 있으면 사용, 없으면 <b> 태그 사용
                    if 'KoreanFont-Bold' in pdfmetrics.getRegisteredFontNames():
                        self.text_buffer.append('<font name="KoreanFont-Bold">')
                    else:
                        self.text_buffer.append('<b>')
                elif tag == 'em' or tag == 'i':
                    self.text_buffer.append('<i>')
                elif tag == 'code':
                    self.text_buffer.append('<font face="Courier" color="#e74c3c"><b>')
            
            def handle_endtag(self, tag):
                if tag == 'h1' or tag == 'h2' or tag == 'h3':
                    self.flush_text()
                    self.story.append(Spacer(1, 0.3*cm))
                    self.current_style = normal_style
                    self.in_paragraph = False
                elif tag == 'p':
                    # <p> 태그 종료 시 버퍼 내용을 flush
                    self.flush_text()
                    self.story.append(Spacer(1, 0.2*cm))
                    self.in_paragraph = False
                elif tag == 'strong' or tag == 'b':
                    # 볼드 폰트가 등록되어 있으면 </font>로 닫기
                    if 'KoreanFont-Bold' in pdfmetrics.getRegisteredFontNames():
                        self.text_buffer.append('</font>')
                    else:
                        self.text_buffer.append('</b>')
                elif tag == 'em' or tag == 'i':
                    self.text_buffer.append('</i>')
                elif tag == 'code':
                    self.text_buffer.append('</b></font>')
            
            def handle_data(self, data):
                # 텍스트 데이터 추가
                # HTML 파서가 이미 태그를 분리했으므로, 순수 텍스트만 받습니다
                # ReportLab의 Paragraph는 HTML을 지원하므로, 태그가 아닌 텍스트의 특수 문자만 이스케이프
                # 하지만 HTML 파서가 이미 태그를 분리했으므로, 여기서 받는 data는 순수 텍스트입니다
                # ReportLab은 &, <, >를 자동으로 처리하지만, 안전을 위해 &만 처리
                if data:
                    # &amp; 같은 엔티티는 그대로 두고, 순수 &만 변환
                    # 하지만 HTML 파서가 이미 엔티티를 디코딩했을 수 있으므로 주의
                    # 실제로는 ReportLab이 자동으로 처리하므로 그대로 추가
                    self.text_buffer.append(data)
            
            def flush_text(self):
                if self.text_buffer:
                    text = ''.join(self.text_buffer)
                    if text.strip():
                        try:
                            # 중첩 태그 정리
                            text = self._clean_html(text)
                            # 디버깅: 생성되는 텍스트 확인
                            print(f"Paragraph 텍스트: {text[:200]}")
                            self.story.append(Paragraph(text, self.current_style))
                        except Exception as e:
                            import traceback
                            print(f"Paragraph 생성 오류: {e}")
                            print(f"텍스트: {text[:100]}")
                            print(traceback.format_exc())
                            # HTML 태그 제거 후 재시도
                            clean_text = re_module.sub(r'<[^>]+>', '', text)
                            try:
                                self.story.append(Paragraph(clean_text, self.current_style))
                            except:
                                # 그래도 실패하면 텍스트만
                                self.story.append(Paragraph(clean_text.replace('&', '&amp;'), self.current_style))
                    self.text_buffer = []
            
            def _clean_html(self, text):
                """HTML 태그를 정리하여 reportlab이 지원하는 형식으로 변환"""
                # 중첩된 태그 제거
                while '<b><b>' in text:
                    text = text.replace('<b><b>', '<b>')
                while '</b></b>' in text:
                    text = text.replace('</b></b>', '</b>')
                while '<i><i>' in text:
                    text = text.replace('<i><i>', '<i>')
                while '</i></i>' in text:
                    text = text.replace('</i></i>', '</i>')
                return text
        
        parser = HTMLToReportLab(story, styles)
        try:
            parser.feed(html_content)
            parser.flush_text()
        except Exception as e:
            import traceback
            print(f"HTML 파싱 오류: {e}")
            print(traceback.format_exc())
            # 파싱 실패 시 기본 텍스트로 추가
            if not story:
                story.append(Paragraph("PDF 생성 중 오류가 발생했습니다.", normal_style))
        
        # story가 비어있으면 기본 내용 추가
        if not story:
            story.append(Paragraph("내용이 없습니다.", normal_style))
        
        # PDF 생성
        try:
            doc.build(story)
        except Exception as e:
            import traceback
            print(f"PDF 빌드 오류: {e}")
            print(traceback.format_exc())
            raise Exception(f'PDF 생성 중 오류가 발생했습니다: {str(e)}')
        
        pdf_buffer.seek(0)
        
        # PDF가 제대로 생성되었는지 확인
        pdf_size = len(pdf_buffer.getvalue())
        if pdf_size == 0:
            raise Exception('PDF 파일이 비어있습니다.')
        
        # PDF 헤더 확인 (PDF 파일은 %PDF로 시작해야 함)
        pdf_buffer.seek(0)
        header = pdf_buffer.read(4)
        pdf_buffer.seek(0)
        if header != b'%PDF':
            raise Exception('생성된 파일이 유효한 PDF가 아닙니다.')
        
        # 파일명 생성 (한글 파일명 지원)
        filename = f"{lecture.lecture_name}_요약.pdf"
        encoded_filename = quote(filename.encode('utf-8'))
        
        # HttpResponse로 PDF 파일 다운로드
        pdf_data = pdf_buffer.read()
        response = HttpResponse(pdf_data, content_type='application/pdf')
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
        response['Content-Length'] = len(pdf_data)
        
        return response
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"요약 PDF 생성 오류: {e}")
        print(error_trace)
        # 에러 발생 시에도 에러 메시지를 포함한 PDF 반환 시도
        try:
            pdf_buffer = BytesIO()
            doc = SimpleDocTemplate(pdf_buffer, pagesize=A4,
                                    rightMargin=2*cm, leftMargin=2*cm,
                                    topMargin=2*cm, bottomMargin=2*cm)
            korean_font = register_korean_font()
            styles = getSampleStyleSheet()
            error_style = ParagraphStyle(
                'ErrorStyle',
                parent=styles['Normal'],
                fontSize=12,
                textColor=HexColor('#e74c3c'),
                fontName=korean_font
            )
            story = [Paragraph(f"PDF 생성 중 오류가 발생했습니다: {str(e)}", error_style)]
            doc.build(story)
            pdf_buffer.seek(0)
            filename = f"{lecture.lecture_name}_요약_오류.pdf"
            encoded_filename = quote(filename.encode('utf-8'))
            pdf_data = pdf_buffer.read()
            response = HttpResponse(pdf_data, content_type='application/pdf')
            response['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
            response['Content-Length'] = len(pdf_data)
            return response
        except:
            # PDF 생성도 실패하면 에러 메시지 반환
            messages.error(request, f'요약 파일 다운로드 중 오류가 발생했습니다: {str(e)}')
            return redirect('lecture_detail', lecture_id=lecture_id)

# 6. 스크립트 파일 다운로드
@login_required
def download_script_view(request, lecture_id):
    """
    강의의 타임스탬프가 포함된 전체 스크립트를 Markdown 형식으로 출력한 후 PDF 파일로 다운로드합니다.
    """
    lecture = get_object_or_404(Lecture, id=lecture_id, user=request.user)
    
    # 스크립트 데이터가 없으면 에러 반환
    if not lecture.full_script:
        messages.error(request, '스크립트 데이터가 없습니다.')
        return redirect('lecture_detail', lecture_id=lecture_id)
    
    try:
        # Markdown 형식으로 내용 생성
        md_content = []
        md_content.append(f"# {lecture.lecture_name}\n\n")
        # 서울 시간대로 변환
        seoul_time = timezone.localtime(lecture.created_at)
        md_content.append(f"**문서 생성일:** {seoul_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        md_content.append("---\n\n")
        md_content.append("## 전체 스크립트 (타임스탬프 포함)\n\n")
        md_content.append("---\n\n")
        
        # 스크립트를 타임스탬프별로 줄바꿈 처리
        script_text = lecture.full_script
        
        # 타임스탬프 패턴: [MM:SS] 또는 [MM:SS - MM:SS]
        timestamp_pattern = re.compile(r'\[(\d{2}):(\d{2})(?:\s*-\s*(\d{2}):(\d{2}))?\]')
        
        # 모든 타임스탬프의 위치 찾기
        matches = list(timestamp_pattern.finditer(script_text))
        
        # 타임스탬프별로 텍스트 분할하고 줄바꿈 추가
        if matches:
            formatted_text = ''
            for i, match in enumerate(matches):
                current_index = match.start()
                next_index = matches[i + 1].start() if i < len(matches) - 1 else len(script_text)
                
                # 현재 타임스탬프부터 다음 타임스탬프 전까지의 텍스트 추출
                segment = script_text[current_index:next_index]
                
                # 타임스탬프 뒤의 공백을 줄바꿈으로 변경 (가독성 향상)
                segment = re.sub(r'\]\s+', ']\n', segment)
                
                # 첫 번째가 아니면 줄바꿈 추가
                if i > 0:
                    formatted_text += '\n\n'
                
                formatted_text += segment
            
            script_text = formatted_text
        else:
            # 타임스탬프가 없어도 타임스탬프 뒤 공백 처리
            script_text = re.sub(r'\]\s+', ']\n', script_text)
        
        # 타임스탬프를 코드 형식으로 변환 (강조 표시)
        script_text = re.sub(r'\[(\d{2}:\d{2}(?:\s*-\s*\d{2}:\d{2})?)\]', r'`[\1]`', script_text)
        
        # 연속된 줄바꿈 정리 (최대 2개까지만 허용)
        script_text = re.sub(r'\n{3,}', '\n\n', script_text)
        
        md_content.append(script_text)
        
        # Markdown을 HTML로 변환
        markdown_text = ''.join(md_content)
        html_content = markdown.markdown(markdown_text, extensions=['extra'])
        
        # 디버깅용: HTML 출력 (서버 콘솔에서 확인)
        print("=== 생성된 HTML (처음 1000자) ===")
        print(html_content[:1000])
        print("=================================")
        
        # reportlab을 사용하여 PDF 생성
        pdf_buffer = BytesIO()
        doc = SimpleDocTemplate(pdf_buffer, pagesize=A4,
                                rightMargin=2*cm, leftMargin=2*cm,
                                topMargin=2*cm, bottomMargin=2*cm)
        
        # 한글 폰트 등록
        korean_font = register_korean_font()
        
        # 스타일 정의
        styles = getSampleStyleSheet()
        
        # 커스텀 스타일 정의 (한글 폰트 사용)
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=HexColor('#2c3e50'),
            spaceAfter=20,
            alignment=TA_LEFT,
            fontName=korean_font
        )
        
        heading2_style = ParagraphStyle(
            'CustomHeading2',
            parent=styles['Heading2'],
            fontSize=18,
            textColor=HexColor('#34495e'),
            spaceBefore=20,
            spaceAfter=12,
            fontName=korean_font
        )
        
        normal_style = ParagraphStyle(
            'CustomNormal',
            parent=styles['Normal'],
            fontSize=11,
            leading=18,
            textColor=HexColor('#333333'),
            spaceAfter=8,
            fontName=korean_font
        )
        
        # HTML을 reportlab 요소로 변환
        story = []
        
        # HTML을 reportlab이 지원하는 형식으로 간단하게 변환
        import re as re_module
        
        # 먼저 <strong> -> <b>, <em> -> <i> 변환 (중첩 태그도 처리)
        # 여러 번 반복하여 중첩된 경우도 처리
        max_iterations = 10
        for _ in range(max_iterations):
            old_content = html_content
            html_content = re_module.sub(r'<strong>(.*?)</strong>', r'<b>\1</b>', html_content, flags=re_module.DOTALL)
            html_content = re_module.sub(r'<em>(.*?)</em>', r'<i>\1</i>', html_content, flags=re_module.DOTALL)
            if old_content == html_content:
                break
        
        # <code> 태그 처리
        html_content = re_module.sub(r'<code>(.*?)</code>', r'<font face="Courier" color="#e74c3c"><b>\1</b></font>', html_content, flags=re_module.DOTALL)
        
        # <hr> 태그는 그대로 유지 (파서에서 처리)
        
        # HTML 파싱하여 요소 생성
        from html.parser import HTMLParser
        
        class HTMLToReportLab(HTMLParser):
            def __init__(self, story, styles):
                super().__init__()
                self.story = story
                self.styles = styles
                self.current_style = normal_style
                self.text_buffer = []
                self.in_paragraph = False
            
            def handle_starttag(self, tag, attrs):
                if tag == 'h1':
                    self.flush_text()
                    self.current_style = title_style
                    self.in_paragraph = True
                elif tag == 'h2':
                    self.flush_text()
                    self.current_style = heading2_style
                    self.in_paragraph = True
                elif tag == 'p':
                    self.flush_text()
                    self.current_style = normal_style
                    self.in_paragraph = True
                elif tag == 'hr':
                    self.flush_text()
                    # 구분선 추가 (더 눈에 띄게)
                    self.story.append(Spacer(1, 0.3*cm))
                    # 구분선을 위한 선 그리기
                    table = Table([['']], colWidths=[16*cm])
                    table.setStyle(TableStyle([
                        ('LINEBELOW', (0, 0), (-1, -1), 1, HexColor('#cccccc')),
                    ]))
                    self.story.append(table)
                    self.story.append(Spacer(1, 0.3*cm))
                elif tag == 'br':
                    self.text_buffer.append('<br/>')
                elif tag == 'strong' or tag == 'b':
                    # ReportLab의 Paragraph는 <b> 태그를 지원하지만, 한글 폰트에 볼드가 없을 수 있음
                    # 볼드 폰트가 등록되어 있으면 사용, 없으면 <b> 태그 사용
                    if 'KoreanFont-Bold' in pdfmetrics.getRegisteredFontNames():
                        self.text_buffer.append('<font name="KoreanFont-Bold">')
                    else:
                        self.text_buffer.append('<b>')
                elif tag == 'em' or tag == 'i':
                    self.text_buffer.append('<i>')
                elif tag == 'code':
                    self.text_buffer.append('<font face="Courier" color="#e74c3c"><b>')
            
            def handle_endtag(self, tag):
                if tag == 'h1' or tag == 'h2':
                    self.flush_text()
                    self.story.append(Spacer(1, 0.3*cm))
                    self.current_style = normal_style
                    self.in_paragraph = False
                elif tag == 'p':
                    self.flush_text()
                    self.story.append(Spacer(1, 0.2*cm))
                    self.in_paragraph = False
                elif tag == 'strong' or tag == 'b':
                    # 볼드 폰트가 등록되어 있으면 </font>로 닫기
                    if 'KoreanFont-Bold' in pdfmetrics.getRegisteredFontNames():
                        self.text_buffer.append('</font>')
                    else:
                        self.text_buffer.append('</b>')
                elif tag == 'em' or tag == 'i':
                    self.text_buffer.append('</i>')
                elif tag == 'code':
                    self.text_buffer.append('</b></font>')
            
            def handle_data(self, data):
                # 텍스트 데이터 추가
                # HTML 파서가 이미 태그를 분리했으므로, 순수 텍스트만 받습니다
                # ReportLab의 Paragraph는 HTML을 지원하므로, 태그가 아닌 텍스트의 특수 문자만 이스케이프
                # 하지만 HTML 파서가 이미 태그를 분리했으므로, 여기서 받는 data는 순수 텍스트입니다
                # ReportLab은 &, <, >를 자동으로 처리하지만, 안전을 위해 &만 처리
                if data:
                    # &amp; 같은 엔티티는 그대로 두고, 순수 &만 변환
                    # 하지만 HTML 파서가 이미 엔티티를 디코딩했을 수 있으므로 주의
                    # 실제로는 ReportLab이 자동으로 처리하므로 그대로 추가
                    self.text_buffer.append(data)
            
            def flush_text(self):
                if self.text_buffer:
                    text = ''.join(self.text_buffer)
                    if text.strip():
                        try:
                            # 중첩 태그 정리
                            text = self._clean_html(text)
                            # 디버깅: 생성되는 텍스트 확인
                            print(f"Paragraph 텍스트: {text[:200]}")
                            self.story.append(Paragraph(text, self.current_style))
                        except Exception as e:
                            import traceback
                            print(f"Paragraph 생성 오류: {e}")
                            print(f"텍스트: {text[:100]}")
                            print(traceback.format_exc())
                            # HTML 태그 제거 후 재시도
                            clean_text = re_module.sub(r'<[^>]+>', '', text)
                            try:
                                self.story.append(Paragraph(clean_text, self.current_style))
                            except:
                                # 그래도 실패하면 텍스트만
                                self.story.append(Paragraph(clean_text.replace('&', '&amp;'), self.current_style))
                    self.text_buffer = []
            
            def _clean_html(self, text):
                """HTML 태그를 정리하여 reportlab이 지원하는 형식으로 변환"""
                # 중첩된 태그 제거
                while '<b><b>' in text:
                    text = text.replace('<b><b>', '<b>')
                while '</b></b>' in text:
                    text = text.replace('</b></b>', '</b>')
                while '<i><i>' in text:
                    text = text.replace('<i><i>', '<i>')
                while '</i></i>' in text:
                    text = text.replace('</i></i>', '</i>')
                return text
        
        parser = HTMLToReportLab(story, styles)
        try:
            parser.feed(html_content)
            parser.flush_text()
        except Exception as e:
            import traceback
            print(f"HTML 파싱 오류: {e}")
            print(traceback.format_exc())
            # 파싱 실패 시 기본 텍스트로 추가
            if not story:
                story.append(Paragraph("PDF 생성 중 오류가 발생했습니다.", normal_style))
        
        # story가 비어있으면 기본 내용 추가
        if not story:
            story.append(Paragraph("내용이 없습니다.", normal_style))
        
        # PDF 생성
        try:
            doc.build(story)
        except Exception as e:
            import traceback
            print(f"PDF 빌드 오류: {e}")
            print(traceback.format_exc())
            raise Exception(f'PDF 생성 중 오류가 발생했습니다: {str(e)}')
        
        pdf_buffer.seek(0)
        
        # PDF가 제대로 생성되었는지 확인
        pdf_size = len(pdf_buffer.getvalue())
        if pdf_size == 0:
            raise Exception('PDF 파일이 비어있습니다.')
        
        # PDF 헤더 확인 (PDF 파일은 %PDF로 시작해야 함)
        pdf_buffer.seek(0)
        header = pdf_buffer.read(4)
        pdf_buffer.seek(0)
        if header != b'%PDF':
            raise Exception('생성된 파일이 유효한 PDF가 아닙니다.')
        
        # 파일명 생성 (한글 파일명 지원)
        filename = f"{lecture.lecture_name}_스크립트.pdf"
        encoded_filename = quote(filename.encode('utf-8'))
        
        # HttpResponse로 PDF 파일 다운로드
        pdf_data = pdf_buffer.read()
        response = HttpResponse(pdf_data, content_type='application/pdf')
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
        response['Content-Length'] = len(pdf_data)
        
        return response
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"스크립트 PDF 생성 오류: {e}")
        print(error_trace)
        # 에러 발생 시에도 에러 메시지를 포함한 PDF 반환 시도
        try:
            pdf_buffer = BytesIO()
            doc = SimpleDocTemplate(pdf_buffer, pagesize=A4,
                                    rightMargin=2*cm, leftMargin=2*cm,
                                    topMargin=2*cm, bottomMargin=2*cm)
            korean_font = register_korean_font()
            styles = getSampleStyleSheet()
            error_style = ParagraphStyle(
                'ErrorStyle',
                parent=styles['Normal'],
                fontSize=12,
                textColor=HexColor('#e74c3c'),
                fontName=korean_font
            )
            story = [Paragraph(f"PDF 생성 중 오류가 발생했습니다: {str(e)}", error_style)]
            doc.build(story)
            pdf_buffer.seek(0)
            filename = f"{lecture.lecture_name}_스크립트_오류.pdf"
            encoded_filename = quote(filename.encode('utf-8'))
            pdf_data = pdf_buffer.read()
            response = HttpResponse(pdf_data, content_type='application/pdf')
            response['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
            response['Content-Length'] = len(pdf_data)
            return response
        except:
            # PDF 생성도 실패하면 에러 메시지 반환
            messages.error(request, f'스크립트 파일 다운로드 중 오류가 발생했습니다: {str(e)}')
            return redirect('lecture_detail', lecture_id=lecture_id)

# 관리자 페이지
@login_required
def admin_dashboard_view(request):
    # is_staff 체크
    if not request.user.is_staff:
        messages.error(request, '관리자 권한이 필요합니다.')
        return redirect('upload')
    
    # ProcessingStats 통계 가져오기
    try:
        processing_stats = ProcessingStats.get_or_create_singleton()
    except Exception:
        processing_stats = None
    
    # 데이터베이스 테이블 정보 가져오기 (models.py에 정의된 모델만)
    table_info = []
    try:
        # lecture 앱의 모든 모델 가져오기
        lecture_app = apps.get_app_config('lecture')
        lecture_models = lecture_app.get_models()
        
        # 각 모델에서 실제 사용하는 필드 정의
        # CustomUser: docstring에 명시된 필드만 (is_superuser, first_name, last_name 등 제외)
        custom_user_used_fields = {'id', 'username', 'email', 'password', 'is_active', 'is_staff', 'last_login', 'date_joined'}
        
        with connection.cursor() as cursor:
            # 모델 기준으로 순회 (모델 이름 우선)
            for model in sorted(lecture_models, key=lambda m: m.__name__):
                table_name = model._meta.db_table
                try:
                    # 테이블의 컬럼 정보 가져오기
                    cursor.execute(f"PRAGMA table_info({table_name})")
                    columns = cursor.fetchall()
                    all_column_names = [col[1] for col in columns]  # col[1]이 컬럼 이름
                    
                    # 모델에서 실제 사용하는 필드만 필터링
                    if model.__name__ == 'CustomUser':
                        # CustomUser의 경우 docstring에 명시된 필드만 사용
                        column_names = [col for col in all_column_names if col in custom_user_used_fields]
                    else:
                        # 다른 모델의 경우 모델에 정의된 필드만 사용
                        # Django 모델의 실제 필드 컬럼 이름 가져오기
                        model_column_names = set()
                        for field in model._meta.get_fields():
                            if isinstance(field, models.Field):
                                # 일반 필드는 column 속성 사용
                                if hasattr(field, 'column'):
                                    model_column_names.add(field.column)
                            elif hasattr(field, 'name') and hasattr(field, 'related_model'):
                                # ForeignKey 필드는 {field_name}_id로 저장됨
                                model_column_names.add(f'{field.name}_id')
                        
                        # id는 항상 포함
                        model_column_names.add('id')
                        column_names = [col for col in all_column_names if col in model_column_names]
                    
                    # 테이블의 튜플 수 가져오기
                    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                    row_count = cursor.fetchone()[0]
                    
                    table_info.append({
                        'name': table_name,
                        'model_name': model.__name__,
                        'columns': column_names,
                        'row_count': row_count
                    })
                except Exception as e:
                    # 특정 테이블 조회 실패 시 건너뛰기
                    continue
    except Exception as e:
        messages.error(request, f'데이터베이스 정보를 가져오는 중 오류가 발생했습니다: {str(e)}')
        table_info = []
    
    context = {
        'processing_stats': processing_stats,
        'table_info': table_info,
    }
    
    return render(request, 'lecture/admin_dashboard.html', context)