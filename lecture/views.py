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
import os
from urllib.parse import quote
from .models import Lecture, ProcessingStats, CustomUser, PdfChunk, Mapping
from .tasks import process_lecture_task, calculate_etr_task, start_process_from_url_task # Celery 태스크 임포트
from .services import init_gemini_models, init_chromadb_client, get_rag_response
import json

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
    강의의 소주제별 요약본을 TXT 파일로 다운로드합니다.
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
        
        # TXT 파일 내용 생성
        txt_content = []
        txt_content.append(f"강의명: {lecture.lecture_name}\n")
        txt_content.append(f"생성일: {lecture.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n")
        txt_content.append("=" * 80 + "\n\n")
        
        for idx, item in enumerate(summary_list, 1):
            txt_content.append(f"[소주제 {idx}] {item.get('topic', '제목 없음')}\n")
            txt_content.append("-" * 80 + "\n")
            
            if item.get('timestamp'):
                txt_content.append(f"타임스탬프: {item.get('timestamp')}\n")
            
            if item.get('topic') in mappings_dict:
                txt_content.append(f"PDF 페이지: {mappings_dict[item.get('topic')]}p\n")
            
            txt_content.append(f"\n요약:\n{item.get('summary', '요약 내용 없음')}\n\n")
            
            if item.get('original_segment'):
                txt_content.append(f"원본 구간:\n{item.get('original_segment')}\n")
            
            txt_content.append("\n" + "=" * 80 + "\n\n")
        
        # 파일명 생성 (한글 파일명 지원)
        filename = f"{lecture.lecture_name}_요약.txt"
        # 파일명을 UTF-8로 인코딩하여 브라우저 호환성 확보
        encoded_filename = quote(filename.encode('utf-8'))
        
        # HttpResponse로 파일 다운로드
        response = HttpResponse(''.join(txt_content), content_type='text/plain; charset=utf-8')
        # RFC 5987 형식으로 파일명 설정 (한글 파일명 지원)
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
        
        return response
        
    except Exception as e:
        messages.error(request, f'요약 파일 다운로드 중 오류가 발생했습니다: {str(e)}')
        return redirect('lecture_detail', lecture_id=lecture_id)

# 6. 스크립트 파일 다운로드
@login_required
def download_script_view(request, lecture_id):
    """
    강의의 타임스탬프가 포함된 전체 스크립트를 TXT 파일로 다운로드합니다.
    """
    lecture = get_object_or_404(Lecture, id=lecture_id, user=request.user)
    
    # 스크립트 데이터가 없으면 에러 반환
    if not lecture.full_script:
        messages.error(request, '스크립트 데이터가 없습니다.')
        return redirect('lecture_detail', lecture_id=lecture_id)
    
    try:
        # TXT 파일 내용 생성
        txt_content = []
        txt_content.append(f"강의명: {lecture.lecture_name}\n")
        txt_content.append(f"생성일: {lecture.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n")
        txt_content.append("=" * 80 + "\n\n")
        txt_content.append("전체 스크립트 (타임스탬프 포함)\n")
        txt_content.append("-" * 80 + "\n\n")
        txt_content.append(lecture.full_script)
        
        # 파일명 생성 (한글 파일명 지원)
        filename = f"{lecture.lecture_name}_스크립트.txt"
        # 파일명을 UTF-8로 인코딩하여 브라우저 호환성 확보
        encoded_filename = quote(filename.encode('utf-8'))
        
        # HttpResponse로 파일 다운로드
        response = HttpResponse(''.join(txt_content), content_type='text/plain; charset=utf-8')
        # RFC 5987 형식으로 파일명 설정 (한글 파일명 지원)
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
        
        return response
        
    except Exception as e:
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