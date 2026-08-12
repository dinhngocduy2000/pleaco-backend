"""Render HTML email bodies from Jinja2 templates under app/external/mail/templates/.

Example::

    from app.common.schemas.mail import SendMailRequest
    from app.core.config import settings
    from app.external.mail.jinja_templates import render_mail_html

    html = render_mail_html(
        "welcome.html",
        name="Alex",
        app_name=settings.APP_NAME,
        action_url="https://example.com/onboarding",
        subject="Welcome to Chronohub",
    )
    request = SendMailRequest(
        to=["alex@example.com"],
        subject="Welcome to Chronohub",
        body="Hi Alex, thanks for joining. Get started: https://example.com/onboarding",
        html=html,
    )
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_mail_html(template_name: str, **context: object) -> str:
    """Load ``template_name`` (e.g. ``welcome.html``) and return rendered HTML."""
    return _env.get_template(template_name).render(**context)
