from django.urls import path
from . import views

urlpatterns = [
    # 인증 관련
    path('login/', views.login_view, name='login'),
    path('signup/', views.signup_view, name='signup'),
    path('logout/', views.logout_view, name='logout'),
    
    # 1. 업로드 페이지 (Streamlit의 upload_view)
    path('', views.upload_view, name='upload'),
    
    # 2. 메인 학습 페이지 (Streamlit의 main_view)
    path('lecture/<int:lecture_id>/', views.lecture_detail_view, name='lecture_detail'),
    
    # 3. RAG 챗봇 API 엔드포인트
    path('api/chat/', views.api_chat_view, name='api_chat'),
    
    # 4. (선택사항) 업로드 상태 폴링 API
    path('api/lecture_status/<int:lecture_id>/', views.api_lecture_status_view, name='api_lecture_status'),
]