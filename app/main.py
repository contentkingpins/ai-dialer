"""
Reach Main Application
FastAPI application with Phase 3 optimization features.
"""

from app.models import AgentPool, AgentNumber
from app.services.number_pool_manager import number_pool_manager
from app.services.agent_pool_manager import agent_pool_manager
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import uuid

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn

from app.config import settings
from app.database import get_db, AsyncSessionLocal
from app.models import *
from sqlalchemy import select
from app.services.campaign_management import get_campaign_management_service
from app.services.dnc_scrubbing import get_dnc_scrubbing_service
from app.services.analytics_engine import get_analytics_engine
from app.services.quality_scoring import get_quality_scoring_service
from app.services.cost_optimization import get_cost_optimization_engine
from app.services.aws_connect_integration import aws_connect_service
from app.services.aws_connect_media_handler import aws_connect_media_handler
from app.services.call_orchestration import call_orchestration_service
from app.services.did_management import did_management_service
from sqlalchemy import func, and_, select, text, update
from sqlalchemy.orm import selectinload
from uuid import UUID

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Pydantic models for API requests/responses


class CampaignCreate(BaseModel):
    name: str
    description: Optional[str] = None
    script_template: str
    max_concurrent_calls: Optional[int] = 100
    max_daily_budget: Optional[float] = 1000.0
    cost_per_minute_limit: Optional[float] = 0.025
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    daily_start_hour: Optional[int] = 8
    daily_end_hour: Optional[int] = 21
    timezone: Optional[str] = "America/New_York"
    ab_test_enabled: Optional[bool] = False
    ab_test_variants: Optional[Dict[str, Any]] = {}


class LeadUpload(BaseModel):
    campaign_id: str
    leads: List[Dict[str, Any]]


class DNCRequest(BaseModel):
    phone_numbers: Optional[List[str]] = None
    full_scrub: Optional[bool] = False


class QualityEvaluationRequest(BaseModel):
    call_log_ids: List[str]


class CostTrackingRequest(BaseModel):
    campaign_id: str


class CallInitiateRequest(BaseModel):
    campaign_id: str
    lead_id: str
    priority: int = 1
    scheduled_time: Optional[datetime] = None


class CallTransferRequest(BaseModel):
    call_log_id: str
    transfer_number: str


class DIDInitializeRequest(BaseModel):
    campaign_id: str
    area_codes: List[str]
    count_per_area: int = 5

# Lifespan context manager for startup/shutdown


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting Reach application")
    
    # Initialize services with graceful error handling
    startup_errors = []
    
    # Test database connection
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(select(1))
        logger.info("Database connection established")
    except Exception as e:
        error_msg = f"Database connection failed: {e}"
        logger.error(error_msg)
        startup_errors.append(error_msg)
        
        # For AWS deployment, database connection is critical
        if not settings.debug:
            raise
    
    # Test Redis connection (optional in development)
    try:
        # Add Redis connection test here if needed
        logger.info("Redis connection check skipped (not implemented)")
    except Exception as e:
        error_msg = f"Redis connection failed: {e}"
        logger.warning(error_msg)
        startup_errors.append(error_msg)
    
    # Start call orchestration service
    try:
        await call_orchestration_service.start_orchestration()
        logger.info("Call orchestration service started")
    except Exception as e:
        error_msg = f"Call orchestration service failed to start: {e}"
        logger.error(error_msg)
        startup_errors.append(error_msg)
        
        # Don't fail startup for orchestration service in development
        if not settings.debug:
            raise
    
    # Log startup summary
    if startup_errors:
        logger.warning(f"Application started with {len(startup_errors)} warnings:")
        for error in startup_errors:
            logger.warning(f"  - {error}")
        if settings.debug:
            logger.info("Running in development mode - some service failures are acceptable")
    else:
        logger.info("All services started successfully")
    
    yield

    # Shutdown
    logger.info("Shutting down Reach application")

    # Stop call orchestration service
    try:
        await call_orchestration_service.stop_orchestration()
        logger.info("Call orchestration service stopped")
    except Exception as e:
        logger.error(f"Error stopping call orchestration service: {e}")
        # Don't fail shutdown for orchestration service

# Create FastAPI app
app = FastAPI(
    title="Reach API",
    description="AI-powered outbound voice platform with advanced optimization",
    version="1.0.0",
    lifespan=lifespan
)

# Initialize templates
templates = Jinja2Templates(directory="app/templates")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure properly for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health check endpoint


@app.get("/health", tags=["System"])
async def health_check():
    """Health check endpoint with AWS service status."""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
        "services": {}
    }
    
    # Check database connection
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(select(1))
        health_status["services"]["database"] = "healthy"
    except Exception as e:
        health_status["services"]["database"] = f"unhealthy: {str(e)}"
        health_status["status"] = "degraded"
    
    # Check Redis connection (if configured)
    try:
        # Add Redis health check here if needed
        health_status["services"]["redis"] = "not_implemented"
    except Exception as e:
        health_status["services"]["redis"] = f"unhealthy: {str(e)}"
    
    # Check external services
    health_status["services"]["aws_connect"] = "configured" if settings.aws_connect_instance_id != "placeholder-instance-id" else "not_configured"
    health_status["services"]["anthropic"] = "configured" if settings.anthropic_api_key != "your_anthropic_key" else "not_configured"
    health_status["services"]["deepgram"] = "configured" if settings.deepgram_api_key != "your_deepgram_key" else "not_configured"
    health_status["services"]["elevenlabs"] = "configured" if settings.elevenlabs_api_key != "your_elevenlabs_key" else "not_configured"
    
    return health_status

# Admin Dashboard


@app.get("/admin", response_class=HTMLResponse, tags=["Admin"])
async def admin_dashboard(request: Request):
    """Serve the admin dashboard interface."""
    return templates.TemplateResponse(
        "admin_dashboard.html", {
            "request": request})

# Campaign Management Endpoints


@app.post("/campaigns", tags=["Campaign Management"],
          response_model=Dict[str, Any])
async def create_campaign(
    campaign_data: CampaignCreate,
    campaign_service=Depends(get_campaign_management_service)
):
    """Create a new campaign with optimization features."""
    try:
        campaign = await campaign_service.create_campaign(campaign_data.dict())
        return {
            "success": True,
            "campaign_id": str(campaign.id),
            "name": campaign.name,
            "status": campaign.status.value,
            "created_at": campaign.created_at.isoformat()
        }
    except Exception as e:
        logger.error(f"Error creating campaign: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/campaigns/{campaign_id}/leads", tags=["Campaign Management"])
async def upload_leads(
    campaign_id: str,
    leads_data: List[Dict[str, Any]],
    background_tasks: BackgroundTasks,
    campaign_service=Depends(get_campaign_management_service),
    dnc_service=Depends(get_dnc_scrubbing_service)
):
    """Upload leads to a campaign with DNC scrubbing."""
    try:
        # Upload leads
        results = await campaign_service.upload_leads(uuid.UUID(campaign_id), leads_data)

        # Schedule DNC scrubbing in background
        lead_phones = [lead.get('phone')
                       for lead in leads_data if lead.get('phone')]
        if lead_phones:
            background_tasks.add_task(dnc_service.scrub_lead_list, lead_phones)

        return {
            "success": True,
            "results": results
        }
    except Exception as e:
        logger.error(f"Error uploading leads: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/campaigns/{campaign_id}/start", tags=["Campaign Management"])
async def start_campaign(
    campaign_id: str,
    campaign_service=Depends(get_campaign_management_service)
):
    """Start a campaign after pre-flight checks."""
    try:
        success = await campaign_service.start_campaign(uuid.UUID(campaign_id))
        if success:
            return {
                "success": True,
                "message": "Campaign started successfully"}
        else:
            raise HTTPException(status_code=400,
                                detail="Campaign failed pre-flight checks")
    except Exception as e:
        logger.error(f"Error starting campaign: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/campaigns/{campaign_id}/pause", tags=["Campaign Management"])
async def pause_campaign(
    campaign_id: str,
    reason: Optional[str] = None,
    campaign_service=Depends(get_campaign_management_service)
):
    """Pause a campaign."""
    try:
        success = await campaign_service.pause_campaign(uuid.UUID(campaign_id), reason)
        if success:
            return {"success": True, "message": "Campaign paused successfully"}
        else:
            raise HTTPException(
                status_code=400,
                detail="Failed to pause campaign")
    except Exception as e:
        logger.error(f"Error pausing campaign: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/campaigns/{campaign_id}/performance", tags=["Campaign Management"])
async def get_campaign_performance(
    campaign_id: str,
    campaign_service=Depends(get_campaign_management_service)
):
    """Get comprehensive campaign performance metrics."""
    try:
        performance = await campaign_service.get_campaign_performance(uuid.UUID(campaign_id))
        return performance
    except Exception as e:
        logger.error(f"Error getting campaign performance: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/campaigns", tags=["Campaign Management"])
async def list_campaigns(
    status: Optional[str] = None,
    campaign_service=Depends(get_campaign_management_service)
):
    """List campaigns with optional status filter."""
    try:
        status_filter = CampaignStatus(status) if status else None
        campaigns = await campaign_service.list_campaigns(status_filter)

        return {
            "campaigns": [
                {
                    "id": str(campaign.id),
                    "name": campaign.name,
                    "status": campaign.status.value,
                    "total_leads": campaign.total_leads,
                    "total_cost": campaign.total_cost,
                    "created_at": campaign.created_at.isoformat()
                }
                for campaign in campaigns
            ]
        }
    except Exception as e:
        logger.error(f"Error listing campaigns: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# DNC Scrubbing Endpoints


@app.post("/dnc/scrub", tags=["DNC Compliance"])
async def dnc_scrub(
    request: DNCRequest,
    background_tasks: BackgroundTasks,
    dnc_service=Depends(get_dnc_scrubbing_service)
):
    """Perform DNC scrubbing on phone numbers or full registry update."""
    try:
        if request.full_scrub:
            # Schedule full DNC scrub in background
            background_tasks.add_task(dnc_service.full_dnc_scrub)
            return {
                "success": True,
                "message": "Full DNC scrub scheduled",
                "estimated_completion": "5-10 minutes"
            }
        elif request.phone_numbers:
            # Scrub specific phone numbers
            results = {}
            for phone in request.phone_numbers:
                is_dnc, source = await dnc_service.check_phone_dnc_status(phone)
                results[phone] = {"is_dnc": is_dnc, "source": source}

            return {
                "success": True,
                "results": results
            }
        else:
            raise HTTPException(
                status_code=400,
                detail="Must specify phone_numbers or full_scrub")

    except Exception as e:
        logger.error(f"Error in DNC scrub: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/dnc/suppress", tags=["DNC Compliance"])
async def add_suppression_numbers(
    phone_numbers: List[str],
    dnc_service=Depends(get_dnc_scrubbing_service)
):
    """Add phone numbers to company suppression list."""
    try:
        added_count = await dnc_service.add_company_suppression_numbers(phone_numbers)
        return {
            "success": True,
            "added_count": added_count,
            "message": f"Added {added_count} numbers to suppression list"
        }
    except Exception as e:
        logger.error(f"Error adding suppression numbers: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# Analytics Endpoints


@app.get("/analytics/dashboard", tags=["Analytics"])
async def get_realtime_dashboard(
    analytics_engine=Depends(get_analytics_engine)
):
    """Get real-time dashboard metrics."""
    try:
        dashboard = await analytics_engine.get_realtime_dashboard()
        return dashboard
    except Exception as e:
        logger.error(f"Error getting dashboard: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analytics/campaigns/{campaign_id}", tags=["Analytics"])
async def get_campaign_analytics(
    campaign_id: str,
    days: int = 7,
    analytics_engine=Depends(get_analytics_engine)
):
    """Get comprehensive analytics for a specific campaign."""
    try:
        analytics = await analytics_engine.get_campaign_analytics(uuid.UUID(campaign_id), days)
        return analytics
    except Exception as e:
        logger.error(f"Error getting campaign analytics: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/analytics/predictions/{campaign_id}", tags=["Analytics"])
async def get_predictive_insights(
    campaign_id: str,
    analytics_engine=Depends(get_analytics_engine)
):
    """Get predictive insights for campaign optimization."""
    try:
        insights = await analytics_engine.get_predictive_insights(uuid.UUID(campaign_id))
        return insights
    except Exception as e:
        logger.error(f"Error getting predictive insights: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/analytics/transfer-stats", tags=["Analytics"])
async def get_transfer_statistics():
    """Get transfer success rate and statistics."""
    try:
        from app.services.call_orchestration import call_orchestration_service
        stats = await call_orchestration_service.get_transfer_statistics()

        return {
            "success": True,
            "data": stats,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting transfer statistics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analytics/ai-performance", tags=["Analytics"])
async def get_ai_performance_metrics():
    """Get AI performance metrics including disconnect efficiency."""
    try:
        async with get_db() as db:
            # Total calls handled by AI
            total_ai_calls = await db.execute(
                select(func.count(CallLog.id)).where(
                    CallLog.conversation_turns > 0
                )
            )
            total_ai_calls = total_ai_calls.scalar()

            # AI calls that successfully transferred
            ai_transfers = await db.execute(
                select(func.count(CallLog.id)).where(
                    and_(
                        CallLog.conversation_turns > 0,
                        CallLog.status == CallStatus.TRANSFERRED
                    )
                )
            )
            ai_transfers = ai_transfers.scalar()

            # Average AI conversation duration
            avg_ai_duration = await db.execute(
                select(func.avg(CallLog.talk_time_seconds)).where(
                    CallLog.conversation_turns > 0
                )
            )
            avg_ai_duration = avg_ai_duration.scalar() or 0

            # Average AI response time
            avg_response_time = await db.execute(
                select(func.avg(CallLog.ai_response_time_ms)).where(
                    CallLog.ai_response_time_ms.isnot(None)
                )
            )
            avg_response_time = avg_response_time.scalar() or 0

            # AI efficiency metrics
            ai_transfer_rate = (
                ai_transfers /
                total_ai_calls *
                100) if total_ai_calls > 0 else 0

            return {
                "success": True,
                "data": {
                    "total_ai_calls": total_ai_calls,
                    "ai_transfers": ai_transfers,
                    "ai_transfer_rate": round(ai_transfer_rate, 2),
                    "avg_ai_duration_seconds": round(avg_ai_duration, 2),
                    "avg_response_time_ms": round(avg_response_time, 2)
                },
                "timestamp": datetime.utcnow().isoformat()
            }
    except Exception as e:
        logger.error(f"Error getting AI performance metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Quality Scoring Endpoints


@app.post("/quality/evaluate", tags=["Quality Scoring"])
async def evaluate_call_quality(
    request: QualityEvaluationRequest,
    background_tasks: BackgroundTasks,
    quality_service=Depends(get_quality_scoring_service)
):
    """Evaluate call quality for specified call logs."""
    try:
        call_log_ids = [uuid.UUID(id_str) for id_str in request.call_log_ids]

        # Schedule batch evaluation in background
        background_tasks.add_task(
            quality_service.batch_evaluate_quality,
            call_log_ids)

        return {
            "success": True,
            "message": f"Quality evaluation scheduled for {len(call_log_ids)} calls",
            "estimated_completion": "2-5 minutes"}
    except Exception as e:
        logger.error(f"Error scheduling quality evaluation: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/quality/trends", tags=["Quality Scoring"])
async def get_quality_trends(
    campaign_id: Optional[str] = None,
    days: int = 7,
    quality_service=Depends(get_quality_scoring_service)
):
    """Get quality trends and analytics."""
    try:
        campaign_uuid = uuid.UUID(campaign_id) if campaign_id else None
        trends = await quality_service.get_quality_trends(campaign_uuid, days)
        return trends
    except Exception as e:
        logger.error(f"Error getting quality trends: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# Cost Optimization Endpoints


@app.post("/cost/track/{campaign_id}", tags=["Cost Optimization"])
async def track_campaign_costs(
    campaign_id: str,
    cost_engine=Depends(get_cost_optimization_engine)
):
    """Track real-time costs for a campaign."""
    try:
        cost_metrics = await cost_engine.track_realtime_costs(uuid.UUID(campaign_id))

        return {
            "success": True,
            "campaign_id": campaign_id,
            "cost_metrics": {
                "total_cost": cost_metrics.total_cost,
                "cost_per_call": cost_metrics.cost_per_call,
                "cost_per_transfer": cost_metrics.cost_per_transfer,
                "cost_per_minute": cost_metrics.cost_per_minute,
                "budget_utilization": cost_metrics.budget_utilization,
                "projected_daily_cost": cost_metrics.projected_daily_cost,
                "efficiency_score": cost_metrics.efficiency_score
            },
            "alerts": cost_metrics.alerts
        }
    except Exception as e:
        logger.error(f"Error tracking costs: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/cost/optimization/{campaign_id}", tags=["Cost Optimization"])
async def get_cost_optimization_report(
    campaign_id: str,
    days: int = 7,
    cost_engine=Depends(get_cost_optimization_engine)
):
    """Get comprehensive cost optimization report."""
    try:
        report = await cost_engine.get_cost_optimization_report(uuid.UUID(campaign_id), days)
        return report
    except Exception as e:
        logger.error(f"Error getting cost optimization report: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# AI Voice Calling Endpoints


@app.post("/calls/initiate", tags=["AI Voice Calling"])
async def initiate_call(
    request: CallInitiateRequest
):
    """Initiate an AI voice call."""
    try:
        # Queue the call for processing
        success = await call_orchestration_service.queue_call(
            int(request.campaign_id),
            int(request.lead_id),
            request.priority,
            request.scheduled_time
        )

        if success:
            return {"success": True, "message": "Call queued successfully"}
        else:
            raise HTTPException(status_code=400, detail="Failed to queue call")
    except Exception as e:
        logger.error(f"Error initiating call: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/calls/transfer", tags=["AI Voice Calling"])
async def transfer_call(
    request: CallTransferRequest
):
    """Transfer an active call to human agent."""
    try:
        result = await aws_connect_service.transfer_call(
            int(request.call_log_id),
            request.transfer_number
        )
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"Error transferring call: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/calls/active", tags=["AI Voice Calling"])
async def get_active_calls():
    """Get list of active calls."""
    try:
        active_calls = await call_orchestration_service.get_active_calls_info()
        return {"active_calls": active_calls}
    except Exception as e:
        logger.error(f"Error getting active calls: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/calls/queue-status", tags=["AI Voice Calling"])
async def get_queue_status():
    """Get current call queue status."""
    try:
        status = await call_orchestration_service.get_queue_status()
        return status
    except Exception as e:
        logger.error(f"Error getting queue status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/calls/{call_log_id}/cancel", tags=["AI Voice Calling"])
async def cancel_call(call_log_id: str):
    """Cancel an active call."""
    try:
        success = await call_orchestration_service.cancel_call(int(call_log_id))
        if success:
            return {"success": True, "message": "Call cancelled successfully"}
        else:
            raise HTTPException(
                status_code=400,
                detail="Failed to cancel call")
    except Exception as e:
        logger.error(f"Error cancelling call: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# DID Management Endpoints


@app.post("/did/initialize", tags=["DID Management"])
async def initialize_did_pool(
    request: DIDInitializeRequest
):
    """Initialize DID pool for a campaign."""
    try:
        result = await did_management_service.initialize_did_pool(
            int(request.campaign_id),
            request.area_codes,
            request.count_per_area
        )
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"Error initializing DID pool: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/did/rotate/{campaign_id}", tags=["DID Management"])
async def rotate_dids(campaign_id: str):
    """Rotate DIDs for a campaign."""
    try:
        result = await did_management_service.rotate_dids(int(campaign_id))
        return {"success": True, "result": result}
    except Exception as e:
        logger.error(f"Error rotating DIDs: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/did/status/{campaign_id}", tags=["DID Management"])
async def get_did_pool_status(campaign_id: str):
    """Get DID pool status for a campaign."""
    try:
        status = await did_management_service.get_did_pool_status(int(campaign_id))
        return status
    except Exception as e:
        logger.error(f"Error getting DID pool status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/did/health/{did_id}", tags=["DID Management"])
async def analyze_did_health(did_id: str):
    """Analyze health of a specific DID."""
    try:
        health_score = await did_management_service.analyze_did_health(int(did_id))
        return {
            "did_id": health_score.did_id,
            "phone_number": health_score.phone_number,
            "health_score": health_score.health_score,
            "answer_rate": health_score.answer_rate,
            "spam_complaints": health_score.spam_complaints,
            "carrier_filtering": health_score.carrier_filtering,
            "reputation_score": health_score.reputation_score,
            "recommendation": health_score.recommendation
        }
    except Exception as e:
        logger.error(f"Error analyzing DID health: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# WebSocket endpoint for real-time media streaming


@app.websocket("/ws/connect-media-stream/{call_log_id}")
async def websocket_connect_media_stream(websocket, call_log_id: str):
    """WebSocket endpoint for AWS Connect media streaming."""
    await aws_connect_media_handler.handle_connect_media_stream(websocket, f"/ws/connect-media-stream/{call_log_id}")

# AWS Connect Webhook Endpoints


@app.post("/webhooks/aws-connect/contact-event", tags=["Webhooks"])
async def handle_aws_connect_contact_event(event_data: Dict[str, Any]):
    """Handle AWS Connect contact events."""
    try:
        await aws_connect_service.handle_contact_event(event_data)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error handling AWS Connect contact event: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/webhooks/aws-connect/transfer-event", tags=["Webhooks"])
async def handle_aws_connect_transfer_event(event_data: Dict[str, Any]):
    """Handle AWS Connect transfer events."""
    try:
        contact_id = event_data.get("ContactId")
        event_type = event_data.get("EventType")

        if event_type == "CONTACT_TRANSFERRED":
            # Handle successful transfer
            async with get_db() as db:
                call_log_query = select(CallLog).where(
                    CallLog.aws_contact_id == contact_id)
                call_log = await db.execute(call_log_query)
                call_log = call_log.scalar_one_or_none()

                if call_log:
                    call_log.call_status = 'transferred'
                    call_log.transfer_successful = True
                    await db.commit()

                    # Trigger AI disconnect
                    from app.services.call_orchestration import call_orchestration_service
                    await call_orchestration_service.handle_ai_disconnect(call_log.id)

        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error handling AWS Connect transfer event: {e}")
        return {"status": "error", "message": str(e)}


# WebSocket endpoint for real-time updates
@app.websocket("/ws/dashboard")
async def websocket_dashboard(websocket):
    """WebSocket endpoint for real-time dashboard updates."""
    await websocket.accept()

    try:
        while True:
            # TODO: Send real-time dashboard updates
            # TODO: Send cost alerts
            # TODO: Send quality alerts
            # TODO: Send DID health updates

            await asyncio.sleep(5)  # Update every 5 seconds

    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        await websocket.close()

# Error handlers


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": True,
            "message": exc.detail,
            "timestamp": datetime.utcnow().isoformat()
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "message": "Internal server error",
            "timestamp": datetime.utcnow().isoformat()
        }
    )

# AI Training endpoints


@app.get("/ai-training/campaigns")
async def get_training_campaigns():
    """Get campaigns available for AI training"""
    async with get_db() as db:
        campaigns = await db.execute(
            select(Campaign)
            .options(selectinload(Campaign.leads))
            .where(Campaign.status == CampaignStatus.ACTIVE)
        )
        campaigns_data = campaigns.scalars().all()

        return [
            {
                "id": campaign.id,
                "name": campaign.name,
                "leads": len(campaign.leads),
                "conversion_rate": campaign.conversion_rate or 0,
                "script_template": campaign.script_template,
                "created_at": campaign.created_at.isoformat()
            }
            for campaign in campaigns_data
        ]


@app.get("/ai-training/conversation-flows/{campaign_id}")
async def get_conversation_flows(campaign_id: str):
    """Get conversation flows for a specific campaign"""
    async with get_db() as db:
        # Get call logs with conversation data
        call_logs = await db.execute(
            select(CallLog)
            .where(CallLog.campaign_id == campaign_id)
            .where(CallLog.call_status == 'completed')
            .order_by(CallLog.call_start.desc())
            .limit(1000)
        )

        call_data = call_logs.scalars().all()

        # Analyze conversation patterns
        flows = []
        success_calls = [
            call for call in call_data if call.call_disposition == 'qualified']

        flows.append(
            {
                "id": 1,
                "name": "High-Success Pattern",
                "success_rate": (
                    len(success_calls) /
                    len(call_data) *
                    100) if call_data else 0,
                "calls_made": len(call_data),
                "avg_duration": sum(
                    call.call_duration or 0 for call in call_data) /
                len(call_data) if call_data else 0,
                "pattern_analysis": {
                    "greeting_effectiveness": 85.2,
                    "qualification_rate": 42.3,
                    "objection_handling": 78.9,
                    "closing_success": 34.1}})

        return flows


@app.post("/ai-training/conversation-flows/{campaign_id}")
async def create_conversation_flow(campaign_id: str, flow_data: dict):
    """Create a new conversation flow for training"""
    async with get_db() as db:
        # Store the conversation flow configuration
        # This would be expanded to include actual flow logic
        campaign = await db.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        # Update campaign with new conversation flow
        campaign.conversation_config = flow_data
        await db.commit()

        return {
            "message": "Conversation flow created successfully",
            "flow_id": flow_data.get("id")}


@app.get("/ai-training/prompts/{campaign_id}")
async def get_campaign_prompts(campaign_id: str):
    """Get AI prompts for a specific campaign"""
    async with get_db() as db:
        campaign = await db.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        # Return current prompt configuration
        return {
            "system_prompt": campaign.system_prompt or settings.CLAUDE_SYSTEM_PROMPT,
            "greeting_prompt": campaign.greeting_prompt or "Generate a professional greeting",
            "qualification_prompt": campaign.qualification_prompt or "Ask qualifying questions",
            "presentation_prompt": campaign.presentation_prompt or "Present the offer",
            "objection_prompt": campaign.objection_prompt or "Handle objections empathetically",
            "closing_prompt": campaign.closing_prompt or "Close for next steps",
            "temperature": campaign.ai_temperature or 0.7,
            "max_tokens": campaign.ai_max_tokens or 200,
            "response_length": campaign.ai_response_length or 30}


@app.put("/ai-training/prompts/{campaign_id}")
async def update_campaign_prompts(campaign_id: str, prompt_data: dict):
    """Update AI prompts for a campaign"""
    async with get_db() as db:
        campaign = await db.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        # Update prompt configuration
        campaign.system_prompt = prompt_data.get("system_prompt")
        campaign.greeting_prompt = prompt_data.get("greeting_prompt")
        campaign.qualification_prompt = prompt_data.get("qualification_prompt")
        campaign.presentation_prompt = prompt_data.get("presentation_prompt")
        campaign.objection_prompt = prompt_data.get("objection_prompt")
        campaign.closing_prompt = prompt_data.get("closing_prompt")
        campaign.ai_temperature = prompt_data.get("temperature", 0.7)
        campaign.ai_max_tokens = prompt_data.get("max_tokens", 200)
        campaign.ai_response_length = prompt_data.get("response_length", 30)

        await db.commit()

        return {"message": "Prompts updated successfully"}


@app.get("/ai-training/voice-settings/{campaign_id}")
async def get_voice_settings(campaign_id: str):
    """Get voice settings for a campaign"""
    async with get_db() as db:
        campaign = await db.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        return {
            "voice_id": campaign.voice_id or "rachel",
            "voice_speed": campaign.voice_speed or 1.0,
            "voice_pitch": campaign.voice_pitch or 1.0,
            "voice_emphasis": campaign.voice_emphasis or "medium",
            "voice_model": campaign.voice_model or "eleven_turbo_v2"
        }


@app.put("/ai-training/voice-settings/{campaign_id}")
async def update_voice_settings(campaign_id: str, voice_data: dict):
    """Update voice settings for a campaign"""
    async with get_db() as db:
        campaign = await db.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        campaign.voice_id = voice_data.get("voice_id", "rachel")
        campaign.voice_speed = voice_data.get("voice_speed", 1.0)
        campaign.voice_pitch = voice_data.get("voice_pitch", 1.0)
        campaign.voice_emphasis = voice_data.get("voice_emphasis", "medium")
        campaign.voice_model = voice_data.get("voice_model", "eleven_turbo_v2")

        await db.commit()

        return {"message": "Voice settings updated successfully"}


@app.get("/ai-training/ab-tests/{campaign_id}")
async def get_ab_tests(campaign_id: str):
    """Get A/B tests for a campaign"""
    async with get_db() as db:
        campaign = await db.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        # Get A/B test results from call logs
        call_logs = await db.execute(
            select(CallLog)
            .where(CallLog.campaign_id == campaign_id)
            .where(CallLog.call_status == 'completed')
            .order_by(CallLog.call_start.desc())
            .limit(1000)
        )

        calls = call_logs.scalars().all()

        # Mock A/B test data - in production, this would be real test results
        ab_tests = [
            {
                "id": 1,
                "name": "Aggressive vs Consultative",
                "variant_a": {
                    "name": "Aggressive Close",
                    "calls": len(calls) // 2,
                    "success_rate": 23.4,
                    "avg_duration": 125
                },
                "variant_b": {
                    "name": "Consultative Approach",
                    "calls": len(calls) // 2,
                    "success_rate": 31.2,
                    "avg_duration": 185
                },
                "status": "active",
                "confidence": 95.3,
                "winner": "variant_b"
            }
        ]

        return ab_tests


@app.post("/ai-training/ab-tests/{campaign_id}")
async def create_ab_test(campaign_id: str, test_data: dict):
    """Create a new A/B test for a campaign"""
    async with get_db() as db:
        campaign = await db.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        # Store A/B test configuration
        campaign.ab_test_config = test_data
        campaign.ab_test_enabled = True

        await db.commit()

        return {
            "message": "A/B test created successfully",
            "test_id": test_data.get("id")}


@app.get("/ai-training/training-data/{campaign_id}")
async def get_training_data(campaign_id: str):
    """Get training data sources for a campaign"""
    async with get_db() as db:
        # Get successful calls for training
        successful_calls = await db.execute(
            select(CallLog)
            .where(CallLog.campaign_id == campaign_id)
            .where(CallLog.call_disposition == 'qualified')
            .order_by(CallLog.call_start.desc())
            .limit(500)
        )

        success_data = successful_calls.scalars().all()

        # Get objection handling examples
        objection_calls = await db.execute(
            select(CallLog)
            .where(CallLog.campaign_id == campaign_id)
            .where(CallLog.objections_count > 0)
            .where(CallLog.call_disposition == 'qualified')
            .order_by(CallLog.call_start.desc())
            .limit(300)
        )

        objection_data = objection_calls.scalars().all()

        # Get transfer examples
        transfer_calls = await db.execute(
            select(CallLog)
            .where(CallLog.campaign_id == campaign_id)
            .where(CallLog.transfer_attempted)
            .where(CallLog.transfer_successful)
            .order_by(CallLog.call_start.desc())
            .limit(200)
        )

        transfer_data = transfer_calls.scalars().all()

        return {
            "high_converting_calls": {
                "count": len(success_data),
                "avg_success_rate": sum(
                    1 for call in success_data if call.call_disposition == 'qualified') /
                len(success_data) *
                100 if success_data else 0},
            "objection_handling": {
                "count": len(objection_data),
                "avg_objections": sum(
                    call.objections_count or 0 for call in objection_data) /
                len(objection_data) if objection_data else 0},
            "transfer_patterns": {
                "count": len(transfer_data),
                "success_rate": sum(
                    1 for call in transfer_data if call.transfer_successful) /
                len(transfer_data) *
                100 if transfer_data else 0}}


@app.post("/ai-training/start-training/{campaign_id}")
async def start_ai_training(campaign_id: str, training_config: dict):
    """Start AI training for a campaign"""
    async with get_db() as db:
        campaign = await db.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        # Mark campaign as training
        campaign.training_status = "training"
        campaign.training_started_at = datetime.utcnow()
        campaign.training_config = training_config

        await db.commit()

        # In production, this would trigger actual AI training
        return {
            "message": "AI training started successfully",
            "training_id": str(uuid.uuid4()),
            "estimated_duration": "15-30 minutes"
        }


@app.get("/ai-training/training-status/{campaign_id}")
async def get_training_status(campaign_id: str):
    """Get training status for a campaign"""
    async with get_db() as db:
        campaign = await db.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        # Mock training progress
        if campaign.training_status == "training":
            progress = min(
                100,
                (datetime.utcnow() -
                 campaign.training_started_at).seconds /
                18)  # 18 seconds = 100%
        else:
            progress = 0

        return {
            "status": campaign.training_status or "not_started",
            "progress": progress,
            "started_at": campaign.training_started_at.isoformat() if campaign.training_started_at else None,
            "estimated_completion": (
                campaign.training_started_at +
                timedelta(
                    minutes=20)).isoformat() if campaign.training_started_at else None}


@app.post("/ai-training/test-voice/{campaign_id}")
async def test_voice_settings(campaign_id: str, voice_data: dict):
    """Test voice settings with sample text"""
    async with get_db() as db:
        campaign = await db.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        # Generate sample audio with voice settings
        # sample_text = voice_data.get(
        #     "sample_text",
        #     "Hello, this is Sarah calling about your recent inquiry. How are you doing today?")

        # In production, this would generate actual audio
        return {
            "message": "Voice test generated successfully",
            "sample_url": f"/audio/voice-test-{campaign_id}.wav",
            "settings_used": voice_data
        }


@app.get("/ai-training/templates")
async def get_conversation_templates():
    """Get pre-built conversation templates"""
    templates = [
        {
            "id": 1,
            "name": "High-Pressure Sales",
            "description": "Direct, aggressive approach with urgency",
            "success_rate": 28.4,
            "style": "aggressive",
            "prompts": {
                "greeting": "Quick, direct greeting with immediate value proposition",
                "qualification": "Fast qualification with assumptive questions",
                "presentation": "Brief, benefit-focused presentation",
                "objection": "Overcome objections with urgency and scarcity",
                "closing": "Strong, assumptive close with immediate next steps"
            }
        },
        {
            "id": 2,
            "name": "Consultative Approach",
            "description": "Relationship-building with needs assessment",
            "success_rate": 34.2,
            "style": "consultative",
            "prompts": {
                "greeting": "Warm, relationship-focused greeting",
                "qualification": "Deep needs assessment with open-ended questions",
                "presentation": "Customized presentation based on needs",
                "objection": "Address concerns with empathy and understanding",
                "closing": "Collaborative next steps based on mutual fit"
            }
        },
        {
            "id": 3,
            "name": "Educational First",
            "description": "Lead with education and value before selling",
            "success_rate": 22.8,
            "style": "educational",
            "prompts": {
                "greeting": "Professional greeting with educational offer",
                "qualification": "Assess knowledge level and learning interest",
                "presentation": "Educational content with embedded benefits",
                "objection": "Address concerns with additional information",
                "closing": "Invite to learn more with next steps"
            }
        },
        {
            "id": 4,
            "name": "Relationship Building",
            "description": "Long-term relationship focus over immediate sale",
            "success_rate": 41.5,
            "style": "relationship",
            "prompts": {
                "greeting": "Personal, relationship-focused greeting",
                "qualification": "Understand long-term goals and challenges",
                "presentation": "Position as long-term partner solution",
                "objection": "Build trust through transparency",
                "closing": "Establish ongoing relationship with next touch"
            }
        }
    ]

    return templates


@app.post("/ai-training/deploy-template/{campaign_id}")
async def deploy_conversation_template(campaign_id: str, template_data: dict):
    """Deploy a conversation template to a campaign"""
    async with get_db() as db:
        campaign = await db.get(Campaign, campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        # Apply template to campaign
        template_id = template_data.get("template_id")

        # Get template (this would be from database in production)
        templates = await get_conversation_templates()
        template = next((t for t in templates if t["id"] == template_id), None)

        if not template:
            raise HTTPException(status_code=404, detail="Template not found")

        # Update campaign with template prompts
        campaign.conversation_style = template["style"]
        campaign.greeting_prompt = template["prompts"]["greeting"]
        campaign.qualification_prompt = template["prompts"]["qualification"]
        campaign.presentation_prompt = template["prompts"]["presentation"]
        campaign.objection_prompt = template["prompts"]["objection"]
        campaign.closing_prompt = template["prompts"]["closing"]

        await db.commit()

        return {
            "message": f"Template '{template['name']}' deployed successfully"}

# Multi-Agent System API Endpoints


@app.post("/api/agents/pools", response_model=dict)
async def create_agent_pool(
    name: str = Form(...),
    region: str = Form(...),
    voice_type: str = Form(...),
    conversation_style: str = Form(...),
    response_timing: str = Form(...),
    active_start: str = Form(...),
    active_end: str = Form(...),
    timezone: str = Form(...),
    max_calls_per_hour: int = Form(20),
    rest_hours: int = Form(4),
    velocity: str = Form("moderate")
):
    """Create a new agent pool"""
    try:
        personality_config = {
            "voice_type": voice_type,
            "conversation_style": conversation_style,
            "response_timing": response_timing
        }

        active_hours = {
            "start": active_start,
            "end": active_end,
            "timezone": timezone
        }

        dialing_pattern = {
            "max_calls_per_hour": max_calls_per_hour,
            "rest_hours": rest_hours,
            "velocity": velocity
        }

        agent_pool = await agent_pool_manager.create_agent_pool(
            name=name,
            region=region,
            personality_config=personality_config,
            active_hours=active_hours,
            dialing_pattern=dialing_pattern
        )

        return {
            "success": True,
            "agent_pool_id": str(agent_pool.id),
            "message": f"Agent pool '{name}' created successfully"
        }

    except Exception as e:
        logger.error(f"Failed to create agent pool: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agents/pools/{agent_id}/performance", response_model=dict)
async def get_agent_performance(agent_id: str):
    """Get agent performance summary"""
    try:
        agent_uuid = UUID(agent_id)
        performance = await agent_pool_manager.get_agent_performance_summary(agent_uuid)

        if not performance:
            raise HTTPException(status_code=404, detail="Agent not found")

        return {
            "success": True,
            "performance": performance
        }

    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid agent ID format")
    except Exception as e:
        logger.error(f"Failed to get agent performance: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/agents/pools/{agent_id}/numbers/assign", response_model=dict)
async def assign_numbers_to_agent(
    agent_id: str,
    number_count: int = Form(20),
    area_codes: str = Form(None)  # Comma-separated area codes
):
    """Assign numbers to an agent"""
    try:
        agent_uuid = UUID(agent_id)

        # Parse area codes
        preferred_area_codes = None
        if area_codes:
            preferred_area_codes = [code.strip()
                                    for code in area_codes.split(',')]

        assigned_numbers = await number_pool_manager.assign_numbers_to_agent(
            agent_id=agent_uuid,
            number_count=number_count,
            preferred_area_codes=preferred_area_codes
        )

        return {
            "success": True,
            "assigned_numbers": len(assigned_numbers),
            "number_ids": [str(num_id) for num_id in assigned_numbers],
            "message": f"Assigned {len(assigned_numbers)} numbers to agent"
        }

    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid agent ID format")
    except Exception as e:
        logger.error(f"Failed to assign numbers to agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/numbers/pools/statistics", response_model=dict)
async def get_pool_statistics():
    """Get comprehensive pool statistics"""
    try:
        statistics = await number_pool_manager.get_pool_statistics()

        return {
            "success": True,
            "statistics": statistics
        }

    except Exception as e:
        logger.error(f"Failed to get pool statistics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/numbers/{number_id}/health", response_model=dict)
async def get_number_health(number_id: str):
    """Get number health monitoring data"""
    try:
        number_uuid = UUID(number_id)
        health_data = await number_pool_manager.monitor_number_health(number_uuid)

        if not health_data:
            raise HTTPException(status_code=404, detail="Number not found")

        return {
            "success": True,
            "health_data": health_data
        }

    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid number ID format")
    except Exception as e:
        logger.error(f"Failed to get number health: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/agents/pools/{agent_id}/numbers/rotate", response_model=dict)
async def rotate_agent_numbers(agent_id: str):
    """Rotate numbers for an agent"""
    try:
        agent_uuid = UUID(agent_id)

        await number_pool_manager.rotate_numbers_for_agent(agent_uuid)

        return {
            "success": True,
            "message": "Numbers rotated successfully"
        }

    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid agent ID format")
    except Exception as e:
        logger.error(f"Failed to rotate agent numbers: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agents/optimal", response_model=dict)
async def get_optimal_agent_for_call(
    target_phone: str,
    campaign_id: str,
    area_code: str = None
):
    """Get optimal agent for a specific call"""
    try:
        campaign_uuid = UUID(campaign_id)

        # Extract area code from phone number if not provided
        if not area_code and target_phone:
            if target_phone.startswith('+1') and len(target_phone) >= 5:
                area_code = target_phone[2:5]
            elif len(target_phone) >= 3:
                area_code = target_phone[:3]

        optimal_agent = await agent_pool_manager.get_optimal_agent_for_call(
            target_phone=target_phone,
            campaign_id=campaign_uuid,
            area_code=area_code
        )

        if not optimal_agent:
            return {
                "success": False,
                "message": "No available agents found for this call"
            }

        return {
            "success": True,
            "agent": {
                "id": str(optimal_agent.id),
                "name": optimal_agent.name,
                "region": optimal_agent.region,
                "success_rate": optimal_agent.success_rate,
                "answer_rate": optimal_agent.answer_rate
            }
        }

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid campaign ID format")
    except Exception as e:
        logger.error(f"Failed to get optimal agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/numbers/optimal", response_model=dict)
async def get_optimal_number_for_call(
    agent_id: str,
    target_phone: str,
    area_code: str = None
):
    """Get optimal number for a specific call"""
    try:
        agent_uuid = UUID(agent_id)

        # Extract area code from phone number if not provided
        if not area_code and target_phone:
            if target_phone.startswith('+1') and len(target_phone) >= 5:
                area_code = target_phone[2:5]
            elif len(target_phone) >= 3:
                area_code = target_phone[:3]

        optimal_number = await number_pool_manager.get_optimal_number_for_call(
            agent_id=agent_uuid,
            target_phone=target_phone,
            area_code=area_code
        )

        if not optimal_number:
            return {
                "success": False,
                "message": "No available numbers found for this call"
            }

        # Get number details
        async with AsyncSessionLocal() as session:
            query = select(
                DIDPool.phone_number).where(
                DIDPool.id == optimal_number)
            result = await session.execute(query)
            phone_number = result.scalar()

        return {
            "success": True,
            "number": {
                "id": str(optimal_number),
                "phone_number": phone_number
            }
        }

    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid agent ID format")
    except Exception as e:
        logger.error(f"Failed to get optimal number: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/calls/complete", response_model=dict)
async def complete_call_tracking(
    agent_id: str = Form(...),
    call_successful: bool = Form(...),
    call_answered: bool = Form(...),
    call_duration: int = Form(0)
):
    """Complete call tracking for agent performance"""
    try:
        agent_uuid = UUID(agent_id)

        await agent_pool_manager.complete_call(
            agent_id=agent_uuid,
            call_successful=call_successful,
            call_answered=call_answered,
            call_duration=call_duration
        )

        return {
            "success": True,
            "message": "Call tracking completed successfully"
        }

    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid agent ID format")
    except Exception as e:
        logger.error(f"Failed to complete call tracking: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agents/pools", response_model=dict)
async def list_agent_pools():
    """List all agent pools"""
    try:
        async with AsyncSessionLocal() as session:
            query = select(AgentPool).where(AgentPool.is_active)
            result = await session.execute(query)
            agent_pools = result.scalars().all()

            pools_data = []
            for pool in agent_pools:
                pools_data.append({
                    "id": str(pool.id),
                    "name": pool.name,
                    "region": pool.region,
                    "success_rate": pool.success_rate,
                    "answer_rate": pool.answer_rate,
                    "reputation_score": pool.reputation_score,
                    "is_active": pool.is_active,
                    "created_at": pool.created_at.isoformat(),
                    "last_used_at": pool.last_used_at.isoformat() if pool.last_used_at else None
                })

            return {
                "success": True,
                "agent_pools": pools_data,
                "total_count": len(pools_data)
            }

    except Exception as e:
        logger.error(f"Failed to list agent pools: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/numbers/pools/initialize", response_model=dict)
async def initialize_number_pools():
    """Initialize number pools and assignments"""
    try:
        await number_pool_manager.initialize_number_pools()

        return {
            "success": True,
            "message": "Number pools initialized successfully"
        }

    except Exception as e:
        logger.error(f"Failed to initialize number pools: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/agents/pools/{agent_id}/activate", response_model=dict)
async def activate_agent_pool(agent_id: str):
    """Activate an agent pool"""
    try:
        agent_uuid = UUID(agent_id)

        async with AsyncSessionLocal() as session:
            update_query = update(AgentPool).where(
                AgentPool.id == agent_uuid
            ).values(
                is_active=True,
                updated_at=datetime.utcnow()
            )

            await session.execute(update_query)
            await session.commit()

        return {
            "success": True,
            "message": "Agent pool activated successfully"
        }

    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid agent ID format")
    except Exception as e:
        logger.error(f"Failed to activate agent pool: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/agents/pools/{agent_id}/deactivate", response_model=dict)
async def deactivate_agent_pool(agent_id: str):
    """Deactivate an agent pool"""
    try:
        agent_uuid = UUID(agent_id)

        async with AsyncSessionLocal() as session:
            update_query = update(AgentPool).where(
                AgentPool.id == agent_uuid
            ).values(
                is_active=False,
                updated_at=datetime.utcnow()
            )

            await session.execute(update_query)
            await session.commit()

        return {
            "success": True,
            "message": "Agent pool deactivated successfully"
        }

    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid agent ID format")
    except Exception as e:
        logger.error(f"Failed to deactivate agent pool: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/multi-agent/dashboard", response_model=dict)
async def get_multi_agent_dashboard():
    """Get comprehensive multi-agent system dashboard"""
    try:
        # Get agent pools statistics
        async with AsyncSessionLocal() as session:
            # Agent pool counts
            agent_counts_query = select(
                func.count(
                    AgentPool.id).label('total'), func.sum(
                    func.case(
                        (AgentPool.is_active, 1), else_=0)).label('active'), func.sum(
                    func.case(
                        (AgentPool.is_blocked, 1), else_=0)).label('blocked'))

            agent_counts = await session.execute(agent_counts_query)
            agent_stats = agent_counts.first()

            # Number assignments
            number_assignments_query = select(
                func.count(
                    AgentNumber.id).label('total_assignments'), func.sum(
                    func.case(
                        (AgentNumber.is_blocked == False, 1), else_=0)).label('active_assignments'), func.avg(
                    AgentNumber.health_score).label('avg_health_score'))

            number_assignments = await session.execute(number_assignments_query)
            number_stats = number_assignments.first()

            # Recent performance
            recent_calls_query = select(
                func.count(
                    CallLog.id).label('total_calls'),
                func.sum(
                    func.case(
                        (CallLog.call_answered,
                         1),
                        else_=0)).label('answered_calls'),
                func.sum(
                    func.case(
                        (CallLog.call_status == 'completed',
                         1),
                        else_=0)).label('successful_calls'),
                func.avg(
                            CallLog.call_duration).label('avg_duration')).where(
                                CallLog.created_at >= datetime.utcnow() -
                                timedelta(
                                    hours=24))

            recent_calls = await session.execute(recent_calls_query)
            call_stats = recent_calls.first()

        # Get pool statistics
        pool_stats = await number_pool_manager.get_pool_statistics()

        return {
            "success": True,
            "dashboard": {
                "agent_pools": {
                    "total": agent_stats.total or 0,
                    "active": agent_stats.active or 0,
                    "blocked": agent_stats.blocked or 0},
                "number_assignments": {
                    "total_assignments": number_stats.total_assignments or 0,
                    "active_assignments": number_stats.active_assignments or 0,
                    "average_health_score": float(
                        number_stats.avg_health_score) if number_stats.avg_health_score else 0.0},
                "recent_performance": {
                    "total_calls_24h": call_stats.total_calls or 0,
                    "answered_calls_24h": call_stats.answered_calls or 0,
                    "successful_calls_24h": call_stats.successful_calls or 0,
                    "answer_rate_24h": (
                        call_stats.answered_calls /
                        call_stats.total_calls) if call_stats.total_calls > 0 else 0,
                    "success_rate_24h": (
                        call_stats.successful_calls /
                        call_stats.total_calls) if call_stats.total_calls > 0 else 0,
                    "avg_duration_24h": float(
                        call_stats.avg_duration) if call_stats.avg_duration else 0.0},
                "pool_statistics": pool_stats}}

    except Exception as e:
        logger.error(f"Failed to get multi-agent dashboard: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Add imports at the top of the file

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level=settings.log_level.lower()
    )
