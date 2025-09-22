#!/usr/bin/env python3
"""
Jenkins MCP AI Assistant Server

A Model Context Protocol server that provides Jenkins CI/CD pipeline failure analysis and automated fixing capabilities.
Supports both stdio and HTTP transports for different deployment scenarios.
"""

import asyncio
import json
import os
import argparse
from typing import Any, Sequence
from mcp.server.models import *
from mcp.server import NotificationOptions, Server
from mcp.types import *
import mcp.types as types
from jenkins_tools import JenkinsAPI, FailureParser, FailureAnalyzer, CodeFixSuggester
from models import JobConfiguration, BuildParameters, BuildLogs, ParsedFailures, FailureAnalysis, FailureEvent, CodeFixSuggestion, PipelineStage, PipelineSubStage, PipelineDescription, StageFailureContext, StageLogs
from config import logger

# Additional imports for HTTP transport
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# Create server instance
server = Server("jenkins-mcp-server")

# Create FastAPI app for HTTP transport
app = FastAPI(
    title="Jenkins MCP AI Assistant",
    description="Model Context Protocol server for Jenkins CI/CD pipeline failure analysis",
    version="1.0.0"
)

# Add CORS middleware for cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request/Response models for HTTP API
class ToolCallRequest(BaseModel):
    tool_name: str
    arguments: dict = {}

class ToolCallResponse(BaseModel):
    success: bool
    result: str
    error: str = None

class JenkinsMCPServer:
    def __init__(self):
        self.jenkins_api = JenkinsAPI()
        self.failure_parser = FailureParser()
        self.failure_analyzer = FailureAnalyzer()
        self.code_fix_suggester = CodeFixSuggester()
    


    async def analyze_build_failure_enhanced(self, jenkins_url: str) -> FailureAnalysis:
        """
        Enhanced build failure analysis using stage-wise pipeline analysis.
        This method targets specific failing stages instead of parsing massive console logs.
        """
        try:
            logger.info("Starting enhanced stage-wise build failure analysis", jenkins_url=jenkins_url)
            
            # 1. Analyze pipeline stages to get targeted failure contexts
            stage_contexts = self.jenkins_api.analyze_pipeline_stages_from_url(jenkins_url)
            
            if not stage_contexts:
                # Check if this is a truly successful build or just no real failures found
                try:
                    pipeline_desc = self.jenkins_api.fetch_pipeline_description(jenkins_url)
                    failed_stages = [stage for stage in pipeline_desc.stages if stage.status in ["FAILED", "ABORTED"]]
                    
                    if not failed_stages:
                        # Truly successful build
                        return FailureAnalysis(
                            category="Build Successful",
                            rootCause="✅ Build completed successfully! All pipeline stages passed without any failures. No analysis required.",
                            confidence=1.0,
                            severity="None", 
                            impactScope="No Impact",
                            isRecurring=False,
                            technicalDetails=f"Pipeline completed with all {len(pipeline_desc.stages)} stages successful. No failed or aborted stages detected.",
                            investigationAreas="No investigation needed - build was successful",
                            blockingFactors="None - build completed successfully", 
                            relatedSystems="All systems functioning properly"
                        )
                    else:
                        # Failed stages exist but no real failures found (all fake/cascade)
                        return FailureAnalysis(
                            category="No Real Failures", 
                            rootCause="No genuine failures found in pipeline stages - all detected failures appear to be fake, cascade, or aborted stages",
                            confidence=1.0,
                            severity="Low",
                            impactScope="Single Build",
                            isRecurring=False,
                            technicalDetails=f"Pipeline has {len(failed_stages)} failed/aborted stages but all were classified as non-actionable (cascade failures or aborted stages)",
                            investigationAreas="Review pipeline configuration for cascade failure patterns",
                            blockingFactors="No real blocking factors identified",
                            relatedSystems="Pipeline stage dependencies"
                        )
                except Exception as e:
                    # Fallback if we can't fetch pipeline description again
                    logger.warning("Failed to re-fetch pipeline description for success check", error=str(e))
                    return FailureAnalysis(
                        category="No Failures",
                        rootCause="No real failures found in pipeline stages analysis",
                        confidence=0.9,
                        severity="Low",
                        impactScope="Single Build", 
                        isRecurring=False,
                        technicalDetails="Stage-wise analysis completed but no actionable failures identified",
                        investigationAreas="Build appears to have completed successfully or with only non-critical issues",
                        blockingFactors="No significant blocking factors identified",
                        relatedSystems="Jenkins pipeline execution"
                    )
            
            # 2. Focus on the first real failure context
            primary_context = stage_contexts[0]
            logger.info("Analyzing primary failure context", 
                       stage_name=primary_context.stage.name,
                       failed_sub_stages=len(primary_context.failed_sub_stages),
                       log_size=primary_context.log_size)
            
            # 3. Create a synthetic failure event for the targeted logs
            synthetic_failure = FailureEvent(
                line=1,
                message=f"Stage '{primary_context.stage.name}' failed with {len(primary_context.failed_sub_stages)} sub-stage failures",
                type="STAGE_FAILURE",
                context={
                    'stage_name': primary_context.stage.name,
                    'stage_id': primary_context.stage.id,
                    'failed_sub_stages': len(primary_context.failed_sub_stages),
                    'log_size': primary_context.log_size,
                    'error_context': primary_context.combined_logs,
                    'search_strategy': 'enhanced_stage_analysis',
                    'total_log_lines': len(primary_context.combined_logs.split('\n')),
                    'stage_error': primary_context.stage.error
                }
            )
            
            # 4. Perform AI analysis on targeted stage logs
            analysis = self.failure_analyzer.analyze_failure_from_url(synthetic_failure, jenkins_url)
            
            # 5. Enhance analysis with stage-specific context
            enhanced_analysis = FailureAnalysis(
                category=analysis.category,
                rootCause=f"[STAGE: {primary_context.stage.name}] {analysis.rootCause}",
                confidence=analysis.confidence,
                severity=analysis.severity,
                impactScope=analysis.impactScope,
                isRecurring=analysis.isRecurring,
                technicalDetails=f"Stage Analysis: {primary_context.stage.name} (ID: {primary_context.stage.id}) failed with {len(primary_context.failed_sub_stages)} sub-stage failures. Log size: {primary_context.log_size:,} characters. Original details: {analysis.technicalDetails}",
                investigationAreas=f"Focus on stage '{primary_context.stage.name}' sub-stages: {[sub.name for sub in primary_context.failed_sub_stages[:3]]}{'...' if len(primary_context.failed_sub_stages) > 3 else ''}. {analysis.investigationAreas}",
                blockingFactors=analysis.blockingFactors,
                relatedSystems=analysis.relatedSystems
            )
            
            logger.info("Enhanced stage-wise analysis completed", 
                       stage=primary_context.stage.name, 
                       category=enhanced_analysis.category,
                       confidence=enhanced_analysis.confidence)
            
            return enhanced_analysis
            
        except Exception as e:
            logger.error("Enhanced stage-wise analysis failed", jenkins_url=jenkins_url, error=str(e))
            # Return error analysis instead of fallback to removed method
            return FailureAnalysis(
                category="Analysis Error",
                rootCause=f"Enhanced analysis failed: {str(e)}. Please check Jenkins connectivity and try again.",
                confidence=0.0,
                severity="High",
                impactScope="Analysis Tool",
                isRecurring=False,
                technicalDetails=f"Enhanced stage-wise analysis encountered an error: {str(e)}",
                investigationAreas="Check Jenkins API connectivity, verify build URL format, ensure build exists",
                blockingFactors="Analysis system failure - manual investigation required",
                relatedSystems="Jenkins API, MCP Analysis Engine"
            )

# Initialize the MCP server instance
jenkins_mcp = JenkinsMCPServer()

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools."""
    return [
        types.Tool(
            name="fetch_job_configuration",
            description="Retrieves complete Jenkins job configuration including pipeline script, repository settings, parameters, triggers, environment, and build steps.",
            inputSchema={
                "type": "object",
                "properties": {
                    "jobName": {
                        "type": "string",
                        "description": "The name of the Jenkins job"
                    }
                },
                "required": ["jobName"]
            }
        ),
        types.Tool(
            name="fetch_build_parameters",
            description="Retrieves all build parameters used in a specific build via Jenkins API.",
            inputSchema={
                "type": "object",
                "properties": {
                    "jobName": {
                        "type": "string",
                        "description": "The name of the Jenkins job"
                    },
                    "buildNumber": {
                        "type": "integer",
                        "description": "The build number"
                    }
                },
                "required": ["jobName", "buildNumber"]
            }
        ),
        types.Tool(
            name="fetch_build_logs",
            description="Fetches the complete console log from Jenkins for a specific build.",
            inputSchema={
                "type": "object",
                "properties": {
                    "jobName": {
                        "type": "string",
                        "description": "The name of the Jenkins job"
                    },
                    "buildNumber": {
                        "type": "integer",
                        "description": "The build number"
                    }
                },
                "required": ["jobName", "buildNumber"]
            }
        ),
        types.Tool(
            name="parse_failure_events",
            description="Parses raw console log text into discrete failure events with optimized multi-strategy parsing for large logs (1-10000 lines).",
            inputSchema={
                "type": "object",
                "properties": {
                    "logText": {
                        "type": "string",
                        "description": "The raw console log text to parse"
                    }
                },
                "required": ["logText"]
            }
        ),

        types.Tool(
            name="analyze_jenkins_build_enhanced",
            description="🚀 ENHANCED Complete Jenkins build failure analysis & fix suggestions: Intelligently fetches and analyzes logs using advanced stage-wise parsing to target specific failing pipeline stages instead of processing massive console logs. Provides faster, more accurate AI-powered diagnosis with detailed failure analysis and automated code fix suggestions. Features smart log filtering (98.7% size reduction), fake failure detection (skips ABORTED stages), early success detection, and enhanced error handling. Accepts full Jenkins URL for any project type (freestyle, pipeline, multibranch). Single comprehensive tool that provides both deep analysis and actionable fixes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "jenkinsUrl": {
                        "type": "string",
                        "description": "Full Jenkins pipeline build URL (e.g., 'https://jenkins.example.com/job/MyPipeline/123/')"
                    }
                },
                "required": ["jenkinsUrl"]
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """Handle tool calls."""
    if arguments is None:
        arguments = {}
    
    try:
        if name == "fetch_job_configuration":
            result = jenkins_mcp.jenkins_api.fetch_job_configuration(arguments["jobName"])
            return [types.TextContent(type="text", text=json.dumps(result.dict(), indent=2))]
        
        elif name == "fetch_build_parameters":
            result = jenkins_mcp.jenkins_api.fetch_build_parameters(
                arguments["jobName"], 
                arguments["buildNumber"]
            )
            return [types.TextContent(type="text", text=json.dumps(result.dict(), indent=2))]
        
        elif name == "fetch_build_logs":
            result = jenkins_mcp.jenkins_api.fetch_build_logs(
                arguments["jobName"], 
                arguments["buildNumber"]
            )
            return [types.TextContent(type="text", text=json.dumps(result.dict(), indent=2))]
        
        elif name == "parse_failure_events":
            result = jenkins_mcp.failure_parser.parse_failure_events(arguments["logText"])
            return [types.TextContent(type="text", text=json.dumps(result.dict(), indent=2))]
        
        elif name == "analyze_jenkins_build_enhanced":
            result = await jenkins_mcp.analyze_build_failure_enhanced(
                arguments["jenkinsUrl"]
            )
            
            # Extract job name and build number from URL for fix suggestion
            job_name = "enhanced_stage_analysis"
            build_number = 0
            try:
                if '/job/' in arguments["jenkinsUrl"]:
                    parts = arguments["jenkinsUrl"].split('/')
                    if parts[-1].isdigit():
                        build_number = int(parts[-1])
                        for i in range(len(parts)-2, -1, -1):
                            if parts[i] != 'job' and not parts[i].isdigit():
                                job_name = parts[i]
                                break
            except:
                pass
            
            # Generate code fix suggestions using the enhanced analysis
            fix_suggestion = jenkins_mcp.code_fix_suggester.suggest_code_fix(
                result, 
                job_name, 
                build_number
            )
            
            # Extract display name from URL for formatting
            display_name = arguments['jenkinsUrl']
            if '/job/' in display_name:
                parts = display_name.split('/')
                if len(parts) >= 2:
                    display_name = f"Build {parts[-1]}" if parts[-1].isdigit() else display_name
            
            # Format the enhanced output
            formatted_output = f"""## 🚀 Enhanced Jenkins Pipeline Analysis & Fix Suggestions

**Jenkins URL:** {arguments['jenkinsUrl']}
**Build:** {display_name}
**Analysis Method:** Stage-Wise Enhanced Analysis

### 🔍 Enhanced Failure Analysis
- **Category:** {result.category}
- **Severity:** {result.severity}
- **Impact Scope:** {result.impactScope}
- **Confidence:** {result.confidence:.2f}
- **Recurring Issue:** {'Yes' if result.isRecurring else 'No'}

### 🎯 Root Cause Analysis
{result.rootCause}

### 🔧 Technical Details
{result.technicalDetails or 'No additional technical details available'}

### 🔍 Investigation Areas
{result.investigationAreas or 'Review build logs and configuration'}

### ⚠️ Blocking Factors
{result.blockingFactors or 'See root cause analysis'}

### 🌐 Related Systems
{result.relatedSystems or 'Impact limited to current build'}

---

## 🛠️ Automated Fix Suggestions

### Fix Assessment
- **Fix Type:** {fix_suggestion.fixType}
- **Automatable:** {'Yes' if fix_suggestion.isAutomatable else 'No'}
- **Fix Location:** {fix_suggestion.fixLocation}

### 💡 Suggested Fix
{fix_suggestion.suggestedFix}

### 📋 Step-by-Step Instructions
"""
            
            for i, step in enumerate(fix_suggestion.stepByStepInstructions, 1):
                formatted_output += f"{i}. {step}\n"
            
            if fix_suggestion.filesToModify:
                formatted_output += f"\n### 📁 Files to Modify\n"
                for file in fix_suggestion.filesToModify:
                    formatted_output += f"- `{file}`\n"
            
            if fix_suggestion.configurationChanges:
                formatted_output += f"\n### ⚙️ Configuration Changes\n"
                for key, value in fix_suggestion.configurationChanges.items():
                    formatted_output += f"- **{key}:** `{value}`\n"
            
            if fix_suggestion.manualActions:
                formatted_output += f"\n### 👤 Manual Actions Required\n"
                for action in fix_suggestion.manualActions:
                    formatted_output += f"- {action}\n"
            
            formatted_output += f"\n---\n*Enhanced Analysis ID: {fix_suggestion.analysisId}*\n*🚀 Powered by Stage-Wise Pipeline Analysis*"
            
            return [types.TextContent(type="text", text=formatted_output)]
        
        else:
            raise ValueError(f"Unknown tool: {name}")
    
    except Exception as e:
        logger.error("Tool call failed", tool=name, error=str(e))
        return [types.TextContent(type="text", text=f"Error: {str(e)}")]

# HTTP API Endpoints
@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "Jenkins MCP AI Assistant Server", "status": "running", "version": "1.0.0"}

@app.get("/health")
async def health_check():
    """Health check for Kubernetes probes"""
    return {"status": "healthy", "timestamp": "2025-08-29"}

@app.post("/tools/call")
async def call_tool_http(request: ToolCallRequest) -> ToolCallResponse:
    """HTTP endpoint for calling MCP tools"""
    try:
        result = await handle_call_tool(request.tool_name, request.arguments)
        
        if result and len(result) > 0:
            return ToolCallResponse(
                success=True,
                result=result[0].text
            )
        else:
            return ToolCallResponse(
                success=False,
                error="No result returned from tool"
            )
            
    except Exception as e:
        logger.error("HTTP tool call failed", tool=request.tool_name, error=str(e))
        return ToolCallResponse(
            success=False,
            error=str(e)
        )

@app.get("/tools/list")
async def list_tools_http():
    """HTTP endpoint for listing available tools"""
    try:
        tools = await handle_list_tools()
        return {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.inputSchema
                }
                for tool in tools
            ]
        }
    except Exception as e:
        logger.error("Failed to list tools", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

async def run_stdio_server():
    """Run the MCP server with stdio transport"""
    from mcp.server.stdio import stdio_server
    
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="jenkins-mcp-server",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                )
            )
        )

async def run_http_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the FastAPI HTTP server"""
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info"
    )
    
    uvicorn_server = uvicorn.Server(config)
    await uvicorn_server.serve()

async def main():
    """Main entry point with transport selection"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Jenkins MCP AI Assistant Server")
    parser.add_argument(
        "--transport", 
        choices=["stdio", "http"],
        default="stdio",
        help="Transport protocol to use (default: stdio)"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0", 
        help="Host to bind HTTP server (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for HTTP server (default: 8000)"
    )
    
    args = parser.parse_args()
    
    if args.transport == "http":
        logger.info("Starting Jenkins MCP server with HTTP transport", host=args.host, port=args.port)
        await run_http_server(host=args.host, port=args.port)
    else:
        logger.info("Starting Jenkins MCP server with stdio transport")
        await run_stdio_server()

if __name__ == "__main__":
    asyncio.run(main())