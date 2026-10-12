"""Rendering and sending saved reports (summary email + CSV of every publication)."""

import csv
import html
import io
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.utils import timezone

from apps.publishing.models import PostTarget, TargetStatus

from .models import Report, ReportFrequency, ReportPeriod
from .overview import build_overview

CSV_FIELDS = (
    "published_at",
    "channel",
    "network",
    "title",
    "url",
    "impressions",
    "reach",
    "likes",
    "comments",
    "shares",
    "saves",
    "clicks",
    "link_clicks",
    "engagements",
    "engagement_rate",
)


def period_range(report: Report, today: date):
    if report.period == ReportPeriod.LAST_7:
        return today - timedelta(days=7), today - timedelta(days=1)
    if report.period == ReportPeriod.PREVIOUS_WEEK:
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if report.period == ReportPeriod.PREVIOUS_MONTH:
        first = today.replace(day=1)
        last = first - timedelta(days=1)
        return last.replace(day=1), last
    return today - timedelta(days=30), today - timedelta(days=1)


def targets(report: Report, d0: date, d1: date):
    ws = report.workspace
    tz = ws.tz
    start = datetime.combine(d0, time.min, tzinfo=tz)
    end = datetime.combine(d1 + timedelta(days=1), time.min, tzinfo=tz)
    qs = PostTarget.objects.filter(
        post__workspace=ws, status=TargetStatus.PUBLISHED, published_at__gte=start, published_at__lt=end
    )
    if report.campaign_id:
        qs = qs.filter(post__campaign_id=report.campaign_id)
    if report.channels:
        qs = qs.filter(channel_id__in=report.channels)
    return qs


def data(report: Report, today: date | None = None):
    ws = report.workspace
    today = today or timezone.now().astimezone(ws.tz).date()
    d0, d1 = period_range(report, today)
    overview = build_overview(ws, targets(report, d0, d1), d0, d1)
    return {
        "report": {"name": report.name, "workspace": ws.name, "period": report.get_period_display()},
        **overview,
    }


def csv_bytes(report: Report, d0: date, d1: date) -> bytes:
    return publications_csv(targets(report, d0, d1), report.workspace.tz)


def publications_csv(qs, tz) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_FIELDS)
    for t in qs.select_related("channel", "post").order_by("published_at"):
        m = t.metrics or {}
        w.writerow(
            [
                t.published_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                t.channel.name,
                t.channel.provider,
                t.effective_title or (t.effective_body or "").strip().split("\n")[0][:120],
                t.external_url,
                *(m.get(k) if m.get(k) is not None else "" for k in CSV_FIELDS[5:12]),
                t.link_clicks,
                m.get("engagements") if m.get("engagements") is not None else "",
                m.get("engagement_rate") if m.get("engagement_rate") is not None else "",
            ]
        )
    return out.getvalue().encode()


def render_email(report: Report, d: dict):
    t = d["totals"]
    period = f"{d['from']} – {d['to']}"
    subject = f"{report.name}: {period}"
    lines = [
        f"{report.workspace.name} · {period}",
        f"Publications: {t['posts']} · Engagements: {t['engagements']} · Impressions: {t['impressions']} · "
        f"Link clicks: {t['link_clicks']}",
        "",
        "By channel:",
        *(
            f"- {c['name']}: {c['posts']} posts, {c['engagements']} engagements, {c['link_clicks']} link clicks"
            for c in d["channels"]
        ),
        "",
        "Top publications:",
        *(f"- {p['title']} ({p['channel']}): {p['url']}" for p in d["top_posts"][:5]),
    ]
    rows = "".join(
        f"<tr><td>{html.escape(c['name'])}</td><td align=right>{c['posts']}</td>"
        f"<td align=right>{c['impressions']}</td><td align=right>{c['engagements']}</td>"
        f"<td align=right>{c['link_clicks']}</td></tr>"
        for c in d["channels"]
    )
    tops = "".join(
        f'<li><a href="{html.escape(p["url"] or "")}">{html.escape(p["title"] or "Untitled")}</a> '
        f"· {html.escape(p['channel'])} · {(p['metrics'] or {}).get('engagements') or 0} engagements</li>"
        for p in d["top_posts"][:5]
    )
    body = f"""
<h2 style="margin:0 0 4px">{html.escape(report.name)}</h2>
<p style="color:#666;margin:0 0 16px">{html.escape(report.workspace.name)} · {period}</p>
<table cellpadding=6 style="border-collapse:collapse;margin-bottom:16px">
<tr><td><b>{t["posts"]}</b><br>publications</td><td><b>{t["engagements"]}</b><br>engagements</td>
<td><b>{t["impressions"]}</b><br>impressions</td><td><b>{t["link_clicks"]}</b><br>link clicks</td></tr></table>
<table cellpadding=6 border=1 style="border-collapse:collapse;border-color:#ddd">
<tr><th align=left>Channel</th><th>Posts</th><th>Impressions</th><th>Engagements</th><th>Link clicks</th></tr>
{rows or "<tr><td colspan=5>Nothing published in this period.</td></tr>"}</table>
<h3>Top publications</h3><ol>{tops or "<li>None</li>"}</ol>
<p style="color:#888;font-size:12px">Every publication with its numbers is in the attached CSV.</p>"""
    if report.share_token:
        url = f"{settings.FRONTEND_URL}/report/{report.share_token}"
        lines += ["", f"Online: {url}"]
        body += f'<p><a href="{url}">Open the report online</a></p>'
    return subject, "\n".join(lines), body


def send(report: Report, today: date | None = None):
    from apps.notifications.senders import DeliveryError, email_configured, send_mail_message

    if not email_configured():
        raise DeliveryError("This server has no email settings (EMAIL_HOST).")
    if not report.recipients:
        raise DeliveryError("The report has no recipients.")
    d = data(report, today)
    subject, text, body = render_email(report, d)
    send_mail_message(
        report.recipients,
        subject,
        text,
        body,
        attachments=[(f"markette-{d['from']}-{d['to']}.csv", csv_bytes(report, d["from"], d["to"]), "text/csv")],
    )
    report.last_sent_at = timezone.now()
    report.last_error = ""
    report.save(update_fields=["last_sent_at", "last_error"])


def due(report: Report, local_now: datetime, hour: int = 8) -> bool:
    if not report.frequency or local_now.hour != hour:
        return False
    if report.frequency == ReportFrequency.WEEKLY and local_now.weekday() != 0:
        return False
    if report.frequency == ReportFrequency.MONTHLY and local_now.day != 1:
        return False
    last = report.last_sent_at.astimezone(local_now.tzinfo).date() if report.last_sent_at else None
    return last != local_now.date()
