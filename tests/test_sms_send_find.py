from callscoot import sms

XML = (
    '<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][1080,2340]">'
    '<node class="android.widget.EditText" resource-id="x:id/compose_message_text" '
    'text="Hello there" bounds="[158,1922][741,2182]" />'
    '<node class="android.view.View" resource-id="Compose:Draft:Send" content-desc="" '
    'bounds="[899,2024][1057,2182]" />'
    '</node></hierarchy>'
)


def test_send_button_and_field_found_by_role_not_position():
    nodes = sms._parse_ui(XML)
    assert sms._center(sms._find_node(nodes, sms._is_send_button), (1, 2)) == ["978", "2103"]
    field = sms._find_node(nodes, sms._is_compose_field)
    assert field["text"] == "Hello there"
    assert sms._center(field, (1, 2)) == ["449", "2052"]


def test_send_button_found_by_label():
    nodes = sms._parse_ui('<hierarchy><node content-desc="Send SMS" bounds="[10,20][30,40]" /></hierarchy>')
    assert sms._center(sms._find_node(nodes, sms._is_send_button), (1, 2)) == ["20", "30"]


def test_unreadable_screen_falls_back_to_configured_position():
    assert sms._parse_ui("not xml") == []
    assert sms._center(sms._find_node([], sms._is_send_button), (657, 944)) == ["657", "944"]
