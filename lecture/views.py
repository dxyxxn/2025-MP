from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import login, authenticate, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.conf import settings
from django.contrib import messages
import os
from .models import Lecture, ProcessingStats
from .tasks import process_lecture_task, calculate_etr_task # Celery 태스크 임포트
from .services import init_gemini_models, init_chromadb_client, get_rag_response
import json

# 로그인 페이지
def login_view(request):
    if request.user.is_authenticated:
        return redirect('upload')
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        
        if username and password:
            user = authenticate(request, username=username, password=password)
            if user is not None:
                login(request, user)
                return redirect('upload')
            else:
                messages.error(request, '아이디 또는 비밀번호가 올바르지 않습니다.')
        else:
            messages.error(request, '아이디와 비밀번호를 모두 입력해주세요.')
    
    return render(request, 'lecture/login.html')

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
        audio_file = request.FILES.get('audio_file')
        pdf_file = request.FILES.get('pdf_file')
        
        # 빈 문자열 체크
        if not lecture_name:
            error_message = "강의 이름을 입력해주세요."
            lectures = Lecture.objects.all().order_by('-created_at')
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
    lecture = get_object_or_404(Lecture, id=lecture_id, user=request.user)
    
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

    context = {
        'lecture': lecture,
        'summary_list': summary_data.get('summary_list') if summary_data else [],
        'mappings': lecture.mappings.all() # (이건 로직 수정 필요)
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
            # 사용자의 강의인지 확인
            lecture = get_object_or_404(Lecture, id=lecture_id, user=request.user)
            
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
        'step_time': step_time  # 현재 단계의 소요 시간
    })