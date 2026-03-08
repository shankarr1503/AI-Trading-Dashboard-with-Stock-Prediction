"""Alerts router and service."""
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from backend.database.session import get_db
from backend.database.models import Alert, AlertType
from backend.market_data.service import market_data_service

router = APIRouter()


class CreateAlertRequest(BaseModel):
    user_id: int = 1
    symbol: str
    alert_type: str
    threshold_value: Optional[float] = None
    notify_email: bool = True
    notify_sms: bool = False


async def check_alert_condition(alert: Alert) -> bool:
    """Check if an alert condition is triggered."""
    try:
        quote = await market_data_service.get_quote(alert.symbol)
        price = quote.get("current_price", 0)
        volume = quote.get("volume", 0)

        if alert.alert_type == AlertType.PRICE_ABOVE and price >= alert.threshold_value:
            return True
        if alert.alert_type == AlertType.PRICE_BELOW and price <= alert.threshold_value:
            return True
        if alert.alert_type == AlertType.VOLUME_SPIKE and volume > alert.threshold_value:
            return True
    except Exception:
        pass
    return False


@router.post("/")
async def create_alert(req: CreateAlertRequest, db: AsyncSession = Depends(get_db)):
    """Create a new price/signal alert."""
    try:
        alert_type_enum = AlertType[req.alert_type.upper()]
    except KeyError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid alert type. Valid types: {[e.value for e in AlertType]}"
        )
    alert = Alert(
        user_id=req.user_id,
        symbol=req.symbol.upper(),
        alert_type=alert_type_enum,
        threshold_value=req.threshold_value,
        notify_email=req.notify_email,
        notify_sms=req.notify_sms,
    )
    db.add(alert)
    await db.flush()
    return {"id": alert.id, "symbol": alert.symbol, "type": req.alert_type, "message": "Alert created"}


@router.get("/")
async def list_alerts(user_id: int = 1, db: AsyncSession = Depends(get_db)):
    """List all alerts for a user."""
    result = await db.execute(
        select(Alert).where(Alert.user_id == user_id, Alert.is_active == True)
    )
    return result.scalars().all()


@router.delete("/{alert_id}")
async def delete_alert(alert_id: int, db: AsyncSession = Depends(get_db)):
    """Delete an alert."""
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_active = False
    return {"message": f"Alert {alert_id} deactivated"}


@router.post("/check")
async def check_alerts(background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """
    Trigger manual check of all active alerts.
    In production this runs automatically via APScheduler.
    """
    result = await db.execute(select(Alert).where(Alert.is_active == True, Alert.is_triggered == False))
    alerts = result.scalars().all()

    triggered = []
    for alert in alerts:
        is_triggered = await check_alert_condition(alert)
        if is_triggered:
            alert.is_triggered = True
            alert.triggered_at = datetime.utcnow()
            triggered.append({
                "alert_id": alert.id,
                "symbol": alert.symbol,
                "type": alert.alert_type.value,
                "threshold": alert.threshold_value,
            })

    return {"checked": len(alerts), "triggered": triggered}
