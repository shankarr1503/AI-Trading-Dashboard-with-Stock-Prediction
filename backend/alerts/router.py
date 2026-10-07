"""Alerts router — scoped to the authenticated user."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.deps import get_current_user
from backend.database.models import Alert, AlertType, User
from backend.database.session import get_db, utcnow
from backend.indicators.service import indicator_service
from backend.market_data.service import market_data_service, validate_symbol

router = APIRouter()
logger = logging.getLogger(__name__)

THRESHOLD_REQUIRED = {AlertType.PRICE_ABOVE, AlertType.PRICE_BELOW, AlertType.VOLUME_SPIKE}
DEFAULT_THRESHOLDS = {AlertType.RSI_OVERBOUGHT: 70.0, AlertType.RSI_OVERSOLD: 30.0}


class CreateAlertRequest(BaseModel):
    symbol: str
    alert_type: str
    threshold_value: Optional[float] = None
    notify_email: bool = True
    notify_sms: bool = False


def _serialize(a: Alert) -> dict:
    return {
        "id": a.id, "symbol": a.symbol, "alert_type": a.alert_type.value, "threshold_value": a.threshold_value,
        "is_active": a.is_active, "is_triggered": a.is_triggered, "triggered_at": a.triggered_at,
        "notify_email": a.notify_email, "notify_sms": a.notify_sms, "created_at": a.created_at,
    }


async def check_alert_condition(alert: Alert) -> bool:
    """Evaluate one alert against live data."""
    t = alert.alert_type
    try:
        if t in (AlertType.PRICE_ABOVE, AlertType.PRICE_BELOW, AlertType.VOLUME_SPIKE):
            quote = await market_data_service.get_quote(alert.symbol)
            if t == AlertType.PRICE_ABOVE:
                return quote["current_price"] >= alert.threshold_value
            if t == AlertType.PRICE_BELOW:
                return quote["current_price"] <= alert.threshold_value
            return quote.get("volume", 0) > alert.threshold_value
        if t in (AlertType.RSI_OVERBOUGHT, AlertType.RSI_OVERSOLD):
            data = await indicator_service.compute_all(alert.symbol, period="6mo", interval="1d")
            rsi = data["current"].get("RSI_14")
            if rsi is None:
                return False
            threshold = alert.threshold_value if alert.threshold_value is not None else DEFAULT_THRESHOLDS[t]
            return rsi >= threshold if t == AlertType.RSI_OVERBOUGHT else rsi <= threshold
        if t in (AlertType.SIGNAL_BUY, AlertType.SIGNAL_SELL):
            from backend.signals.service import signal_service

            signal = (await signal_service.generate_signal(alert.symbol))["signal"]
            return signal == ("BUY" if t == AlertType.SIGNAL_BUY else "SELL")
    except (ValueError, KeyError) as e:
        logger.info("Alert %s check skipped: %s", alert.id, e)
    return False


@router.post("/", status_code=201)
async def create_alert(req: CreateAlertRequest, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        alert_type = AlertType[req.alert_type.upper()]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Invalid alert type. Valid types: {[e.value for e in AlertType]}")
    if alert_type in THRESHOLD_REQUIRED and req.threshold_value is None:
        raise HTTPException(status_code=400, detail=f"{alert_type.value} requires threshold_value")
    try:
        symbol = validate_symbol(req.symbol)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    alert = Alert(user_id=user.id, symbol=symbol, alert_type=alert_type, threshold_value=req.threshold_value,
                  notify_email=req.notify_email, notify_sms=req.notify_sms)
    db.add(alert)
    await db.flush()
    return _serialize(alert)


@router.get("/")
async def list_alerts(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(
        select(Alert).where(Alert.user_id == user.id, Alert.is_active.is_(True)).order_by(Alert.id)
    )).scalars().all()
    return [_serialize(a) for a in rows]


@router.delete("/{alert_id}")
async def delete_alert(alert_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    alert = await db.get(Alert, alert_id)
    if alert is None or alert.user_id != user.id:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_active = False
    return {"message": f"Alert {alert_id} deactivated"}


@router.post("/check")
async def check_alerts(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Evaluate the current user's active, untriggered alerts now."""
    alerts = (await db.execute(
        select(Alert).where(Alert.user_id == user.id, Alert.is_active.is_(True), Alert.is_triggered.is_(False))
    )).scalars().all()
    triggered = []
    for alert in alerts:
        if await check_alert_condition(alert):
            alert.is_triggered = True
            alert.triggered_at = utcnow()
            triggered.append({"alert_id": alert.id, "symbol": alert.symbol,
                              "type": alert.alert_type.value, "threshold": alert.threshold_value})
    return {"checked": len(alerts), "triggered": triggered}
