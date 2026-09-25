"""
FastAPI wrapper around mpin.py — no authentication.
Every endpoint is open. The URL itself is the only gate.

Endpoints
---------
GET  /                     -> endpoint list
GET  /health               -> health check
GET  /vouchers             -> predefined brands catalog
GET  /session              -> current login state
POST /login/send-otp       -> { phone }
POST /login/verify-otp     -> { phone, otp }
POST /logout               -> clear session
POST /buy                  -> full purchase flow
GET  /docs                 -> Swagger UI
"""

import io
import time
import json
import contextlib
import traceback
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import mpin  # your file, unchanged

app = FastAPI(
    title="Magicpin API",
    description="REST wrapper around mpin.py — login, buy, logout.",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Capture mpin.emit() output as JSON instead of printing it
# ---------------------------------------------------------------------------
def _capture_emit(fn, *args, **kwargs):
    buf = io.StringIO()
    captured = {}
    original_emit = mpin.emit

    def fake_emit(result):
        captured["result"] = result
        return result

    mpin.emit = fake_emit
    try:
        with contextlib.redirect_stdout(buf):
            rc = fn(*args, **kwargs)
    finally:
        mpin.emit = original_emit

    return captured.get("result"), buf.getvalue(), rc


# ---------------------------------------------------------------------------
# Shared engine (single-user session)
# ---------------------------------------------------------------------------
_engine: Optional[mpin.Magicpin] = None


def get_engine(quiet: bool = True, proxy: Optional[str] = None) -> mpin.Magicpin:
    global _engine
    if _engine is None:
        _engine = mpin.Magicpin(quiet=quiet, proxy=proxy)
    return _engine


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class SendOTPReq(BaseModel):
    phone: str
    proxy: Optional[str] = None


class VerifyOTPReq(BaseModel):
    phone: str
    otp: str
    proxy: Optional[str] = None


class BuyReq(BaseModel):
    brand: str = "1"
    url: Optional[str] = None
    amt: Optional[float] = None
    qty: int = 1
    card: str
    phone: Optional[str] = None
    otp: Optional[str] = None
    proxy: Optional[str] = None
    fresh: bool = False
    wait_unlock: bool = False
    unlock_mins: int = 360


# ---------------------------------------------------------------------------
# Info endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "service": "Magicpin API",
        "endpoints": [
            "GET  /health",
            "GET  /session",
            "GET  /vouchers",
            "POST /login/send-otp",
            "POST /login/verify-otp",
            "POST /buy",
            "POST /logout",
            "GET  /docs",
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "mpin-railway-api", "ts": int(time.time())}


@app.get("/session")
def session_info():
    eng = get_engine()
    return {
        "authed":       eng.authed(),
        "phone":        eng.phone,
        "user_id":      eng.user_id,
        "has_token":    bool(eng.auth_token),
        "proxy_egress": getattr(eng, "_egress_fingerprint", ""),
        "direct_ip":    getattr(eng, "_direct_fingerprint", ""),
    }


@app.get("/vouchers")
def list_vouchers():
    return {
        "count":    len(mpin.VOUCHERS_CATALOG),
        "vouchers": mpin.VOUCHERS_CATALOG,
    }


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------
@app.post("/logout")
def logout():
    global _engine
    try:
        if _engine:
            _engine.reset_session()
        _engine = None
        return {"success": True, "message": "logged out"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# OTP flow
# ---------------------------------------------------------------------------
@app.post("/login/send-otp")
def send_otp(req: SendOTPReq):
    try:
        eng = get_engine(quiet=True, proxy=req.proxy)
        resp = eng.send_otp(req.phone)
        return {"success": True, "response": resp}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/login/verify-otp")
def verify_otp(req: VerifyOTPReq):
    try:
        eng = get_engine(quiet=True, proxy=req.proxy)
        resp = eng.verify_otp(req.phone, req.otp)
        eng.save_session()
        return {
            "success":  eng.authed(),
            "authed":   eng.authed(),
            "user_id":  eng.user_id,
            "phone":    eng.phone,
            "response": resp,
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Buy
# ---------------------------------------------------------------------------
@app.post("/buy")
def buy(req: BuyReq):
    t0 = time.perf_counter()

    opts = {
        "url":         req.url or "",
        "amt":         str(req.amt) if req.amt else "",
        "qty":         str(req.qty),
        "card":        req.card,
        "phone":       req.phone or "",
        "otp":         req.otp or "",
        "proxy":       req.proxy or "",
        "fresh":       req.fresh,
        "wait_unlock": req.wait_unlock,
        "unlock_mins": str(req.unlock_mins),
        "quiet":       True,
    }

    pos = [req.brand] if req.brand else []

    try:
        result, stdout_text, rc = _capture_emit(mpin._flow, opts, pos, True)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={
                "success":    False,
                "error":      str(e),
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            },
        )

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if result is None:
        result = {"Response": "NO_RESULT", "stdout_tail": stdout_text[-500:]}

    return {
        "success":     rc == 0,
        "elapsed_ms":  elapsed_ms,
        "result":      result,
        "stdout_tail": stdout_text[-800:] if not result else "",
    }
