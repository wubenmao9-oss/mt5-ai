# -*- coding: utf-8 -*-
"""QQ邮箱预警模块 — smtplib + HTML美化模板"""

import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def _build_close_html(time_str: str, direction: str, reason: str, pnl: float, hold: str) -> str:
    # 动态判断盈利还是亏损并匹配主题颜色
    is_profit = pnl >= 0
    header_bg = "linear-gradient(135deg, #11998e 0%, #38ef7d 100%)" if is_profit else "linear-gradient(135deg, #cb2d3e 0%, #ef473a 100%)"
    pnl_color = "#11998e" if is_profit else "#cb2d3e"
    title_cn = "✅ Closed - Profit" if is_profit else "❌ Closed - Loss"

    direction_cn = "BUY" if direction.lower() in ("buy", "long", "买", "多") else ("SELL" if direction.lower() in ("sell", "short", "卖", "空") else direction)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<body style="font-family: 'Microsoft YaHei', Arial, sans-serif; background-color: #f4f7f6; padding: 30px 10px; margin: 0;">
    <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.08);">
        <div style="background: {header_bg}; padding: 30px 20px; text-align: center; color: white;">
            <h2 style="margin: 0; font-size: 24px; font-weight: 600; letter-spacing: 1px;">{title_cn}</h2>
            <p style="margin: 8px 0 0 0; font-size: 14px; opacity: 0.9;">CatMT5 EA</p>
        </div>
        
        <div style="padding: 30px 40px;">
            <table style="width: 100%; border-collapse: collapse; font-size: 15px; color: #333;">
                <tr style="border-bottom: 1px solid #f0f0f0;">
                    <td style="padding: 15px 0; color: #888; width: 35%;">Close Time</td>
                    <td style="padding: 15px 0; font-weight: 600; text-align: right;">{time_str}</td>
                </tr>
                <tr style="border-bottom: 1px solid #f0f0f0;">
                    <td style="padding: 15px 0; color: #888;">Direction</td>
                    <td style="padding: 15px 0; font-weight: 500; text-align: right;">{direction_cn}</td>
                </tr>
                <tr style="border-bottom: 1px solid #f0f0f0;">
                    <td style="padding: 15px 0; color: #888;">Reason</td>
                    <td style="padding: 15px 0; font-weight: 500; text-align: right;">{reason}</td>
                </tr>
                <tr style="border-bottom: 1px solid #f0f0f0;">
                    <td style="padding: 15px 0; color: #888;">PnL</td>
                    <td style="padding: 15px 0; font-weight: bold; font-size: 18px; color: {pnl_color}; text-align: right;">{pnl:+.2f} USD</td>
                </tr>
                <tr>
                    <td style="padding: 15px 0; color: #888;">Holding</td>
                    <td style="padding: 15px 0; font-weight: 500; text-align: right;">{hold}</td>
                </tr>
            </table>
        </div>
        
        <div style="background-color: #f8f9fa; padding: 20px; text-align: center; color: #999; font-size: 12px;">
            <p style="margin: 0 0 5px 0;">此为系统自动发送的交易通知，请勿直接回复。</p>
            <p style="margin: 0;">© 2026 CatMT5 EA. 版权所有.</p>
        </div>
    </div>
</body>
</html>"""


def _build_order_html(time_str: str, direction: str, strategy: str, volume: float, price: float = 0.0) -> str:
    # 动态判断Direction并匹配主题颜色
    is_buy = direction.lower() in ("buy", "long", "买", "多")
    header_bg = "linear-gradient(135deg, #11998e 0%, #38ef7d 100%)" if is_buy else "linear-gradient(135deg, #cb2d3e 0%, #ef473a 100%)"
    direction_color = "#11998e" if is_buy else "#cb2d3e"
    direction_cn = "BUY (Long)" if is_buy else "SELL (Short)"
    icon = "📈" if is_buy else "▼"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<body style="font-family: 'Microsoft YaHei', Arial, sans-serif; background-color: #f4f7f6; padding: 30px 10px; margin: 0;">
    <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.08);">
        <div style="background: {header_bg}; padding: 30px 20px; text-align: center; color: white;">
            <h2 style="margin: 0; font-size: 24px; font-weight: 600; letter-spacing: 1px;">{icon} Order Executed</h2>
            <p style="margin: 8px 0 0 0; font-size: 14px; opacity: 0.9;">CatMT5 EA</p>
        </div>
        
        <div style="padding: 30px 40px;">
            <table style="width: 100%; border-collapse: collapse; font-size: 15px; color: #333;">
                <tr style="border-bottom: 1px solid #f0f0f0;">
                    <td style="padding: 15px 0; color: #888; width: 35%;">Time</td>
                    <td style="padding: 15px 0; font-weight: 600; text-align: right;">{time_str}</td>
                </tr>
                <tr style="border-bottom: 1px solid #f0f0f0;">
                    <td style="padding: 15px 0; color: #888;">Direction</td>
                    <td style="padding: 15px 0; font-weight: bold; color: {direction_color}; text-align: right;">{direction_cn}</td>
                </tr>
                <tr style="border-bottom: 1px solid #f0f0f0;">
                    <td style="padding: 15px 0; color: #888;">Strategy</td>
                    <td style="padding: 15px 0; font-weight: 500; text-align: right;">{strategy}</td>
                </tr>
                <tr>
                    <td style="padding: 15px 0; color: #888;">Lots</td>
                    <td style="padding: 15px 0; font-weight: bold; font-size: 16px; text-align: right;">{volume:.2f}  lot(s)</td>
                </tr>
            </table>
        </div>
        
        <div style="background-color: #f8f9fa; padding: 20px; text-align: center; color: #999; font-size: 12px;">
            <p style="margin: 0 0 5px 0;">此为系统自动发送的交易通知，请勿直接回复。</p>
            <p style="margin: 0;">© 2026 CatMT5 EA. 版权所有.</p>
        </div>
    </div>
</body>
</html>"""


def _build_silent_html(reason: str, last_time: str, status: dict) -> str:
    # 静默预警采用日落黄/橙色渐变，起到警示作用
    header_bg = "linear-gradient(135deg, #f6d365 0%, #fda085 100%)"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<body style="font-family: 'Microsoft YaHei', Arial, sans-serif; background-color: #f4f7f6; padding: 30px 10px; margin: 0;">
    <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.08);">
        <div style="background: {header_bg}; padding: 30px 20px; text-align: center; color: white;">
            <h2 style="margin: 0; font-size: 24px; font-weight: 600; letter-spacing: 1px;">! Silence Warning</h2>
            <p style="margin: 8px 0 0 0; font-size: 14px; opacity: 0.9;">CatMT5 EA</p>
        </div>
        
        <div style="padding: 30px 40px;">
            <div style="background: #fff8e1; border-left: 4px solid #ffc107; padding: 12px 15px; margin-bottom: 25px; border-radius: 0 4px 4px 0;">
                <p style="margin: 0; font-size: 14px; color: #d35400;">No trades for extended period. Silence monitor triggered.</p>
            </div>
            
            <table style="width: 100%; border-collapse: collapse; font-size: 15px; color: #333;">
                <tr style="border-bottom: 1px solid #f0f0f0;">
                    <td style="padding: 12px 0; color: #888; width: 35%;">Last Action</td>
                    <td style="padding: 12px 0; font-weight: 500; text-align: right;">{last_time}</td>
                </tr>
                <tr style="border-bottom: 1px solid #f0f0f0;">
                    <td style="padding: 12px 0; color: #888;">Engine State</td>
                    <td style="padding: 12px 0; font-weight: 500; text-align: right;">{status.get("state", "未知")}</td>
                </tr>
                <tr style="border-bottom: 1px solid #f0f0f0;">
                    <td style="padding: 12px 0; color: #888;">Position</td>
                    <td style="padding: 12px 0; font-weight: 500; text-align: right;">{"In Position" if status.get("position") else "No Position"}</td>
                </tr>
                <tr>
                    <td style="padding: 15px 0; color: #888;">Trigger Reason</td>
                    <td style="padding: 15px 0; font-weight: bold; color: #e67e22; text-align: right;">{reason}</td>
                </tr>
            </table>
        </div>
        
        <div style="background-color: #f8f9fa; padding: 20px; text-align: center; color: #999; font-size: 12px;">
            <p style="margin: 0 0 5px 0;">Auto safety notice. Do not reply.</p>
            <p style="margin: 0;">© 2026 CatMT5 EA. 版权所有.</p>
        </div>
    </div>
</body>
</html>"""


class EmailAlerter:
    """QQ邮箱 SMTP 报警器封装"""

    def __init__(self, sender: str, password: str, receiver: str,
                 smtp_host: str = "smtp.qq.com", smtp_port: int = 587) -> None:
        self._sender = sender
        self._password = password
        self._receiver = receiver
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._enabled = bool(sender and password)
        if self._enabled:
            logger.info("邮件报警已激活: %s -> %s", sender, receiver)
        else:
            logger.info("邮件服务未配置，请检查 .env 中的 EMAIL_* 变量")

    def send_order(self, time_str: str, direction: str, strategy: str, volume: float = 0.01, price: float = 0.0) -> None:
        if not self._enabled: return
        subj = f"[CatMT5 EA]Order - {direction.upper()}"
        self._send(subj, _build_order_html(time_str, direction, strategy, volume, price))

    def send_close(self, time_str, direction, reason, pnl, hold=""):
        if not self._enabled: return
        subj = f"[CatMT5 EA]平仓通知 - {reason}"
        self._send(subj, _build_close_html(time_str, direction, reason, pnl, hold))

    def send_silent_warning(self, reason: str, last_time: str, status: dict) -> None:
        if not self._enabled: return
        state_str = status.get('state', 'Unknown')
        subj = f"[CatMT5 EA]静默预警 - {state_str}"
        self._send(subj, _build_silent_html(reason, last_time, status))

    def _send(self, subject: str, html: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        # 将发件人昵称定制为系统名称
        msg["From"] = formataddr((Header("笨猫MT5交易系统", 'utf-8').encode(), self._sender))
        msg["To"] = self._receiver
        msg.attach(MIMEText(html, "html", "utf-8"))
        try:
            with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=15) as s:
                s.starttls()
                s.login(self._sender, self._password)
                s.send_message(msg)
            logger.info("邮件发送成功: %s", subject)
        except Exception as e:
            logger.warning("邮件发送失败: %s", e)