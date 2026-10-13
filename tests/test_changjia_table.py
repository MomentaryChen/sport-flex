from sportflex.providers.changjia.provider import _interpret_table, _slot

WALL = " \n<h3>請登入以獲得更佳體驗</h3>"
COURTS = (
    "<div id='BILLIARD_table'><h5><span>2</span>筆結果</h5>"
    "<form id='XDSC_2F2BBilliard01_Form'><input name='LSID' value='2F2BBilliard01'></form></div>"
)
EMPTY = "<div><h5><span>0</span>筆結果</h5><p>沒有場地</p></div>"
LOGIN_PAGE = "<html><body><h1>請登入</h1><form id='login'></form></body></html>"
LOGIN_ONLY = "<html><body><p>請登入後再查看這個場地，頁面還有其他說明。</p></body></html>"


def test_guest_burst_stub_is_retried():
    assert _interpret_table(WALL) == "retry"
    assert _interpret_table("<h3>請登入以獲得更佳體驗</h3>") == "retry"


def test_court_list_is_ok_even_if_it_mentions_login():
    assert _interpret_table(COURTS) == "ok"
    assert _interpret_table(COURTS + "請登入以獲得更佳體驗") == "ok"
    assert _interpret_table(EMPTY) == "ok"


def test_real_login_page_is_not_a_retry():
    # A login form is a real page, not the short guest stub.
    assert _interpret_table(LOGIN_PAGE) == "ok"
    assert _interpret_table(LOGIN_ONLY) == "login"


def test_my_reservation_is_not_bookable_by_us():
    mine = _slot({"Time": "19:00 - 20:00", "allowBooking": "N", "IsMyReservation": "Y", "Price": "490", "Status": "已預約"})
    assert mine.mine and not mine.bookable and mine.price == "490"
    free = _slot({"Time": "20:00 - 21:00", "allowBooking": "Y", "IsMyReservation": "N", "Price": "290"})
    assert free.bookable and not free.mine


def test_unrecognized_body_is_empty():
    assert _interpret_table("") == "empty"
    assert _interpret_table("<p>維護中</p>") == "empty"
