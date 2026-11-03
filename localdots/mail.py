"""自分宛てにメールを送る(SMTP over SSL)。設定は環境変数から読み、リポジトリには置かない。

    LOCALDOTS_SMTP_USER       送信に使うアカウント(Gmail ならそのアドレス)
    LOCALDOTS_SMTP_PASSWORD   パスワード(Gmail なら「アプリ パスワード」)
    LOCALDOTS_MAIL_TO         宛先(省略時は LOCALDOTS_SMTP_USER)
    LOCALDOTS_SMTP_HOST       省略時 smtp.gmail.com
    LOCALDOTS_SMTP_PORT       省略時 465
"""

from __future__ import annotations

import html
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass
class MailSettings:
    host: str
    port: int
    user: str
    password: str
    to: str


def settings_from_env() -> MailSettings | None:
    user = os.environ.get("LOCALDOTS_SMTP_USER")
    password = os.environ.get("LOCALDOTS_SMTP_PASSWORD")
    if not user or not password:
        return None
    return MailSettings(
        host=os.environ.get("LOCALDOTS_SMTP_HOST", "smtp.gmail.com"),
        port=int(os.environ.get("LOCALDOTS_SMTP_PORT", "465")),
        user=user,
        password=password,
        to=os.environ.get("LOCALDOTS_MAIL_TO", user),
    )


def send_markdown(subject: str, markdown: str, settings: MailSettings | None = None) -> str:
    """Markdown を本文(テキストと簡単な HTML の両方)にして送る。送れなかった理由か "sent" を返す。"""
    settings = settings or settings_from_env()
    if settings is None:
        return "メールの設定がありません(LOCALDOTS_SMTP_USER / LOCALDOTS_SMTP_PASSWORD)"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.user
    msg["To"] = settings.to
    msg.set_content(markdown)
    msg.add_alternative(markdown_to_html(markdown), subtype="html")
    with smtplib.SMTP_SSL(settings.host, settings.port, context=ssl.create_default_context(), timeout=60) as s:
        s.login(settings.user, settings.password)
        s.send_message(msg)
    return "sent"


_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s\"]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)  # ここで & も &amp; になるので、URL はもう一度エスケープしない
    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    return _BOLD.sub(r"<b>\1</b>", text)


def markdown_to_html(markdown: str) -> str:
    """ダイジェストで使う範囲(見出し・段落・箇条書き・太字・リンク・区切り線)だけを HTML にする。"""
    out, in_list, para = [], False, []

    def flush_para():
        if para:
            out.append(f"<p>{'<br>'.join(_inline(l) for l in para)}</p>")
            para.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        is_item = stripped.startswith("- ")
        if in_list and not is_item:
            out.append("</ul>")
            in_list = False
        if not stripped:
            flush_para()
        elif m := re.match(r"(#{1,4}) (.*)", stripped):
            flush_para()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif stripped == "---":
            flush_para()
            out.append("<hr>")
        elif is_item:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(stripped[2:])}</li>")
        else:
            para.append(stripped)
    flush_para()
    if in_list:
        out.append("</ul>")
    body = "\n".join(out)
    return f'<html><body style="font-family: sans-serif; line-height: 1.6; max-width: 46em">{body}</body></html>'
