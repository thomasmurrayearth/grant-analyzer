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


CONSULTING_URL = os.environ.get(
    "CONSULTING_URL", "https://thomasmurray.earth/startup-journey.html"
).strip()
CONSULTING_EMAIL = os.environ.get("CONSULTING_EMAIL", "thomasmurraynz@gmail.com").strip()


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
        noun = "opportunity" if grants_found == 1 else "opportunities"
        top5 = sorted(opportunities, key=lambda x: x.get("priority_score", 0) or 0, reverse=True)[:5]

        # Tier colours from the app's design system (frontend/index.html).
        tier_colours = {
            "Must Pursue":               "#2E7055",
            "Big Bet":                   "#1A1916",
            "Quick Win":                 "#3D8C68",
            "Prepare for Next Window":   "#8A6F3A",
            "Strategic Positioning":     "#5F6B47",
            "Low Priority":              "#7A7870",
        }

        rows_html = ""
        for opp in top5:
            tier   = opp.get("priority_tier", "")
            colour = tier_colours.get(tier, "#7A7870")
            score  = opp.get("priority_score")
            score_str = f"{score:.2f}" if isinstance(score, (int, float)) else str(score or "—")
            rows_html += f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #E4E2DE;font-size:13px;">
                {_esc(opp.get('name',''))}
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #E4E2DE;font-size:13px;color:#7A7870;">
                {_esc(opp.get('geography',''))}
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #E4E2DE;font-size:13px;font-weight:700;text-align:center;">
                {score_str}
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #E4E2DE;font-size:12px;">
                <span style="background:{colour};color:#fff;padding:2px 8px;border-radius:3px;white-space:nowrap;">
                  {_esc(tier)}
                </span>
              </td>
            </tr>"""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F7F5F0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1A1916;">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:32px auto;">
    <tr>
      <td style="background:#1A1916;padding:28px 32px;border-radius:6px 6px 0 0;">
        <p style="margin:0;color:#B4B1A9;font-size:12px;text-transform:uppercase;letter-spacing:0.08em;">Grant Analyser</p>
        <h1 style="margin:8px 0 0;color:#F7F5F0;font-size:22px;font-weight:700;">Your analysis is ready</h1>
        <p style="margin:6px 0 0;color:#4FA97E;font-size:15px;">{_esc(company_name)}</p>
      </td>
    </tr>
    <tr>
      <td style="background:#FDFCFA;padding:32px;border:1px solid #E4E2DE;border-top:none;">
        <p style="margin:0 0 24px;font-size:15px;">
          We found <strong>{grants_found} grant {noun}</strong> for {_esc(company_name)}.
        </p>

        <h2 style="margin:0 0 12px;font-size:12px;color:#7A7870;text-transform:uppercase;letter-spacing:0.08em;">Top opportunities</h2>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #E4E2DE;border-radius:6px;overflow:hidden;">
          <thead>
            <tr style="background:#EEECEA;">
              <th style="padding:10px 12px;text-align:left;font-size:12px;color:#7A7870;border-bottom:1px solid #E4E2DE;">Grant</th>
              <th style="padding:10px 12px;text-align:left;font-size:12px;color:#7A7870;border-bottom:1px solid #E4E2DE;">Geography</th>
              <th style="padding:10px 12px;text-align:center;font-size:12px;color:#7A7870;border-bottom:1px solid #E4E2DE;">Score</th>
              <th style="padding:10px 12px;text-align:left;font-size:12px;color:#7A7870;border-bottom:1px solid #E4E2DE;">Tier</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>

        <div style="margin:28px 0;padding:16px 20px;background:rgba(61,140,104,.08);border:1px solid rgba(61,140,104,.3);border-radius:6px;">
          <p style="margin:0;font-size:14px;color:#2E7055;">
            Return to the app to see the full scored list, strategic recommendations, and download your results as an Excel file.
          </p>
        </div>

        <div style="text-align:center;margin-top:24px;">
          <a href="{app_url}" style="display:inline-block;background:#1A1916;color:#F7F5F0;padding:13px 32px;border-radius:4px;text-decoration:none;font-weight:600;font-size:14px;">
            View full results →
          </a>
        </div>

        <div style="margin-top:32px;padding-top:24px;border-top:1px solid #E4E2DE;">
          <h2 style="margin:0 0 8px;font-size:12px;color:#7A7870;text-transform:uppercase;letter-spacing:0.08em;">Want help winning a grant?</h2>
          <p style="margin:0 0 14px;font-size:14px;line-height:1.6;">
            I'm Thomas Murray. I help climate and deep tech startups win grant funding and achieve
            profitability. This tool is a productised version of an opportunity assessment I've done
            for several startups. If you want help turning a grant opportunity into a complete
            application, contact me.
          </p>
          <a href="{CONSULTING_URL}" style="color:#2E7055;font-weight:600;font-size:14px;text-decoration:underline;">
            Get help with your application →
          </a>
        </div>
      </td>
    </tr>
    <tr>
      <td style="padding:20px 32px;text-align:center;">
        <p style="margin:0;font-size:11px;color:#B4B1A9;">
          You received this email because you asked to be notified when your grant analysis was complete.<br>
          This is a one-time service notification — we only email you again if you asked for grant deadline updates.
        </p>
      </td>
    </tr>
  </table>
</body>
</html>"""

        resend.Emails.send({
            "from":    from_addr,
            "to":      [to_email],
            "subject": f"Your grant analysis for {company_name} is ready — {grants_found} {noun} found",
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
