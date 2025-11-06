class DuplicateLectureNameException(Exception):
    """강의 이름이 중복될 때 발생하는 예외"""
    def __init__(self, lecture_name):
        self.lecture_name = lecture_name
        self.message = f"강의 이름 '{lecture_name}'은(는) 이미 존재합니다. 다른 이름을 사용해주세요."
        super().__init__(self.message)

