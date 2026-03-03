"""支付相关 API 路由。"""

import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import payment_crud
from app.db.database import get_db
from app.models.payment import (
    BalanceResponse,
    CreateOrderRequest,
    FreeRemainingResponse,
    OrderStatusResponse,
)
from app.payment.signature import SignatureValidator

router = APIRouter(prefix="/api/payment", tags=["payment"])

# 环境变量
PAY302_APP_ID = os.getenv("PAY302_APP_ID", "ccff86524c")
PAY302_SECRET = os.getenv("PAY302_SECRET", "")
PAY302_API_URL = os.getenv("PAY302_API_URL", "https://api.302.ai/v1/checkout")
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "5"))
CREDIT_PRICE_CENTS = int(os.getenv("CREDIT_PRICE_CENTS", "100"))
CREDITS_PER_PURCHASE = int(os.getenv("CREDITS_PER_PURCHASE", "10"))


def _get_client_ip(request: Request) -> str:
    """获取客户端 IP，优先读取代理头。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _get_validator() -> SignatureValidator:
    """获取签名验证器。未配置 secret 时抛出异常。"""
    if not PAY302_SECRET:
        raise HTTPException(status_code=503, detail="支付服务未配置")
    return SignatureValidator(PAY302_SECRET)


# ============ 创建订单 ============


@router.post("/create")
async def create_order(
    payload: CreateOrderRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """创建支付订单并调用 302 API 获取 checkout_url。"""
    validator = _get_validator()
    client_ip = _get_client_ip(request)

    credits_to_buy = payload.credits or CREDITS_PER_PURCHASE
    amount_cents = (credits_to_buy * CREDIT_PRICE_CENTS) // CREDITS_PER_PURCHASE

    # 先在本地创建订单
    order = await payment_crud.create_order(
        db,
        user_identifier=client_ip,
        amount_cents=amount_cents,
        credits=credits_to_buy,
    )

    # 调用 302 API 创建 checkout
    checkout_params = {
        "app_id": PAY302_APP_ID,
        "amount": amount_cents,
        "currency": "USD",
        "order_id": order.id,
        "description": f"MeetSpot {credits_to_buy} credits",
    }
    checkout_params["sign"] = validator.generate_signature(checkout_params)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(PAY302_API_URL, json=checkout_params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"支付网关错误: {e}")

    checkout_id = data.get("checkout_id", "")
    checkout_url = data.get("checkout_url", "")

    # 回写 checkout_id
    if checkout_id:
        order.pay302_checkout_id = checkout_id
        await db.commit()

    return {
        "success": True,
        "order_id": order.id,
        "checkout_id": checkout_id,
        "checkout_url": checkout_url,
        "credits": credits_to_buy,
        "amount_cents": amount_cents,
    }


# ============ Webhook 回调 ============


@router.post("/webhook")
async def payment_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """接收 302 支付回调，验签后更新订单并充值 credits。"""
    validator = _get_validator()

    body = await request.json()
    signature = body.pop("sign", "") or body.pop("signature", "")

    if not validator.validate(body, signature):
        raise HTTPException(status_code=403, detail="签名验证失败")

    checkout_id = body.get("checkout_id", "")
    payment_status = body.get("status", "")
    pay302_payment_order = body.get("payment_order", "")

    if not checkout_id:
        raise HTTPException(status_code=400, detail="缺少 checkout_id")

    order = await payment_crud.get_order_by_checkout_id(db, checkout_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    # 已处理过的订单不重复处理
    if order.status == "paid":
        return {"success": True, "message": "已处理"}

    if payment_status == "paid":
        await payment_crud.update_order_status(
            db, checkout_id, "paid", pay302_payment_order
        )
        await payment_crud.add_credits(
            db,
            user_identifier=order.user_identifier,
            amount=order.credits,
            order_id=order.id,
            description=f"购买 {order.credits} credits",
        )
    else:
        await payment_crud.update_order_status(db, checkout_id, payment_status)

    return {"success": True}


# ============ 查询接口 ============


@router.get("/status/{checkout_id}", response_model=OrderStatusResponse)
async def get_order_status(
    checkout_id: str, db: AsyncSession = Depends(get_db)
):
    """查询订单状态。"""
    order = await payment_crud.get_order_by_checkout_id(db, checkout_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return OrderStatusResponse(
        order_id=order.id,
        status=order.status,
        credits=order.credits,
        amount_cents=order.amount_cents,
        created_at=order.created_at,
        paid_at=order.paid_at,
    )


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(request: Request, db: AsyncSession = Depends(get_db)):
    """查询当前用户 credits 余额（按 IP 识别）。"""
    client_ip = _get_client_ip(request)
    bal = await payment_crud.get_or_create_balance(db, client_ip)
    return BalanceResponse(
        user_identifier=client_ip,
        balance=bal.balance,
        total_purchased=bal.total_purchased,
        total_consumed=bal.total_consumed,
    )


@router.get("/free-remaining", response_model=FreeRemainingResponse)
async def get_free_remaining(request: Request, db: AsyncSession = Depends(get_db)):
    """查询今日剩余免费次数。"""
    client_ip = _get_client_ip(request)
    used = await payment_crud.get_free_usage_today(db, client_ip)
    remaining = max(0, FREE_DAILY_LIMIT - used)
    return FreeRemainingResponse(
        remaining=remaining,
        daily_limit=FREE_DAILY_LIMIT,
        used_today=used,
    )
