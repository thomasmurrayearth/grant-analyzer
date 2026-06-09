"""
Transactional email notifications via Resend.

Required environment variables:
  RESEND_API_KEY     — API key from resend.com
  RESEND_FROM_EMAIL  — verified sender address, e.g. "Grant Analyser <no-reply@yourdomain.com>"
  APP_URL            — public URL of the app (used for the "View results" link)

If either RESEND_API_KEY or RESEND_FROM_EMAIL is not set, sending is silently
skipped — the app continues to work, just without email notifications.
"""

import logging
import os

logger = logging.getLogger(__name__)


def send_results_email(
    to_email: str,
    company_name: str,
    grants_found: int,
    result: dict,
    job_id: str,
) -> None:
    api_key   = os.environ.get("RESEND_API_KEY", "").strip()
    from_addr = os.environ.get("RESEND_FROM_EMAIL", "").strip()
    app_url   = os.environ.get("APP_URL", "https://grant-analyzer-production.up.railway.app").rstrip("/")

    if not api_key or not from_addr:
        logger.info("Resend not configured — skipping email notification")
        return

    try:
        import resend
        resend.api_key = api_key

        opportunities = result.get("opportunities", [])
        top5 = sorted(opportunities, key=lambda x: x.get("priority_score", 0) or 0, reverse=True)[:5]

        tier_colours = {
            "Must Pursue":               "#1A7A4A",
            "Big Bet":                   "#6B21A8",
            "Quick Win":                 "#1D4ED8",
            "Prepare for Next Window":   "#0F766E",
            "Strategic Positioning":     "#B45309",
            "Low Priority":              "#6B7280",
        }

        rows_html = ""
        for opp in top5:
            tier   = opp.get("priority_tier", "")
            colour = tier_colours.get(tier, "#6B7280")
            score  = opp.get("priority_score")
            score_str = f"{score:.2f}" if isinstance(score, (int, float)) else str(score or "—")
            rows_html += f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;font-size:13px;">
                {_esc(opp.get('name',''))}
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;font-size:13px;color:#64748b;">
                {_esc(opp.get('geography',''))}
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;font-size:13px;font-weight:700;text-align:center;">
                {score_str}
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;font-size:12px;">
                <span style="background:{colour};color:#fff;padding:2px 8px;border-radius:999px;white-space:nowrap;">
                  {_esc(tier)}
                </span>
              </td>
            </tr>"""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1e293b;">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:32px auto;">
    <tr>
      <td style="background:#1E3A5F;padding:28px 32px;border-radius:12px 12px 0 0;">
        <p style="margin:0;color:#94a3b8;font-size:12px;text-transform:uppercase;letter-spacing:0.08em;">Grant Opportunity Analyser</p>
        <h1 style="margin:8px 0 0;color:#fff;font-size:22px;font-weight:700;">Your analysis is ready</h1>
        <p style="margin:6px 0 0;color:#93c5fd;font-size:15px;">{_esc(company_name)}</p>
      </td>
    </tr>
    <tr>
      <td style="background:#fff;padding:32px;border:1px solid #e2e8f0;border-top:none;">
        <p style="margin:0 0 24px;font-size:15px;">
          We found <strong>{grants_found} grant {"opportunity" if grants_found == 1 else "opportunities"}</strong> for {_esc(company_name)}.
        </p>

        <h2 style="margin:0 0 12px;font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:0.08em;">Top opportunities</h2>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
          <thead>
            <tr style="background:#f8fafc;">
              <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;border-bottom:1px solid #e2e8f0;">Grant</th>
              <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;border-bottom:1px solid #e2e8f0;">Geography</th>
              <th style="padding:10px 12px;text-align:center;font-size:12px;color:#64748b;border-bottom:1px solid #e2e8f0;">Score</th>
              <th style="padding:10px 12px;text-align:left;font-size:12px;color:#64748b;border-bottom:1px solid #e2e8f0;">Tier</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>

        <div style="margin:28px 0;padding:16px 20px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;">
          <p style="margin:0;font-size:14px;color:#166534;">
            Return to the app to see the full scored list, strategic recommendations, and download your results as an Excel file.
          </p>
        </div>

        <div style="text-align:center;margin-top:24px;">
          <a href="{app_url}" style="display:inline-block;background:#1E3A5F;color:#fff;padding:13px 32px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;">
            View Full Results →
          </a>
        </div>
      </td>
    </tr>
    <tr>
      <td style="padding:20px 32px;text-align:center;">
        <p style="margin:0;font-size:11px;color:#94a3b8;">
          You received this email because you requested a notification when your grant analysis was complete.<br>
          This is a one-time service notification — you will not receive further emails unless you request another analysis.
        </p>
      </td>
    </tr>
  </table>
</body>
</html>"""

        resend.Emails.send({
            "from":    from_addr,
            "to":      [to_email],
            "subject": f"Your grant analysis for {company_name} is ready — {grants_found} {"opportunity" if grants_found == 1 else "opportunities"} found",
            "html":    html,
        })
        logger.info("Results email sent to %s for job %s", to_email, job_id)

    except Exception as exc:
        logger.warning("Failed to send results email: %s", exc)


def _esc(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
