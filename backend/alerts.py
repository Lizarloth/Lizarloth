import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os


SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)


def send_price_alert(
    to_email: str,
    product_name: str,
    site: str,
    current_price: float,
    threshold: float,
    url: str,
):
    if not SMTP_USER or not SMTP_PASS:
        print(f"⚠️ Email not configured — skipping alert for {product_name}")
        return False

    subject = f"🔔 PricEdge Alert: {product_name} dropped to €{current_price:.2f}"

    html = f"""
    <div style="font-family: sans-serif; max-width: 560px; margin: auto; background: #0d0d0d; color: #e8e6e0; padding: 32px; border-radius: 8px;">
      <div style="font-family: monospace; font-size: 18px; color: #c8f060; margin-bottom: 24px;">PRICELENS ALERT</div>
      <h2 style="margin: 0 0 8px; font-size: 16px;">{product_name}</h2>
      <p style="color: #888; font-size: 13px; margin: 0 0 24px;">Source: {site}</p>

      <div style="background: #1a1a1a; border-radius: 6px; padding: 20px; margin-bottom: 24px;">
        <div style="font-family: monospace; font-size: 32px; color: #60f0a0;">€{current_price:.2f}</div>
        <div style="font-size: 12px; color: #555; margin-top: 4px;">your threshold was €{threshold:.2f}</div>
      </div>

      <a href="{url}" style="display:inline-block; background: #c8f060; color: #0d0d0d; font-family: monospace;
         font-weight: 600; padding: 12px 24px; border-radius: 4px; text-decoration: none;">
        View product →
      </a>

      <p style="font-size: 11px; color: #444; margin-top: 32px;">
        Sent by PricEdge · <a href="https://pricedge.com" style="color:#555;">pricedge.com</a>
      </p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_EMAIL, to_email, msg.as_string())
        print(f"✅ Alert email sent to {to_email} for {product_name}")
        return True
    except Exception as e:
        print(f"❌ Failed to send email: {e}")
        return False


def check_and_alert(product, latest_price: float, db, Alert):
    """Check if a product has crossed its alert threshold and fire email if so."""
    if not product.alert_threshold or latest_price is None:
        return
    if latest_price <= product.alert_threshold:
        sent = send_price_alert(
            to_email=product.alert_email or SMTP_USER,
            product_name=product.name,
            site=product.site,
            current_price=latest_price,
            threshold=product.alert_threshold,
            url=product.url,
        )
        alert = Alert(
            product_id=product.id,
            product_name=product.name,
            site=product.site,
            condition="price_below",
            threshold=product.alert_threshold,
            current_price=latest_price,
            email_sent=sent,
        )
        db.add(alert)
        db.commit()
