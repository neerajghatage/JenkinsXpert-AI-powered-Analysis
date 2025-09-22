from typing import Dict, List, Optional, Any, Union
from pydantic import BaseModel

class RepositoryConfig(BaseModel):
    url: str
    credentialsId: Optional[str] = None
    branches: List[str] = []

class ParameterDefinition(BaseModel):
    name: str
    type: str
    defaultValue: Optional[str] = None
    description: Optional[str] = None

class TriggerConfig(BaseModel):
    githubHook: bool = False
    pollSCM: Optional[str] = None
    buildPeriodically: Optional[str] = None
    remoteTrigger: bool = False

class EnvironmentConfig(BaseModel):
    deleteWorkspace: bool = False
    useSecrets: bool = False
    addTimestamps: bool = False
    timeoutMinutes: Optional[int] = None

class JobConfiguration(BaseModel):
    """Complete Jenkins job configuration"""
    repository: Optional[RepositoryConfig] = None
    parameterDefinitions: List[ParameterDefinition] = []
    triggers: Optional[TriggerConfig] = None
    environment: Optional[EnvironmentConfig] = None
    buildSteps: List[str] = []
    scriptText: str = ""  # Pipeline script or build steps combined
    description: Optional[str] = None
    concurrent: bool = False
    throttle: bool = False

class PipelineScript(BaseModel):
    scriptText: str

class BuildParameters(BaseModel):
    parameters: Dict[str, Any]

class BuildLogs(BaseModel):
    logText: str

class FailureEvent(BaseModel):
    line: int
    message: str
    type: str
    context: Dict[str, Any]

class ParsedFailures(BaseModel):
    failures: List[FailureEvent]

class FailureAnalysis(BaseModel):
    category: str
    rootCause: str
    confidence: float
    severity: Optional[str] = "Medium"
    impactScope: Optional[str] = "Single Build"
    isRecurring: Optional[bool] = False
    technicalDetails: Optional[str] = None
    investigationAreas: Optional[str] = None
    blockingFactors: Optional[str] = None
    relatedSystems: Optional[str] = None

class CodeFixSuggestion(BaseModel):
    analysisId: str
    fixType: str  # "Configuration|Code|Infrastructure|Manual"
    isAutomatable: bool
    suggestedFix: Optional[str] = None
    fixLocation: Optional[str] = "Unknown"
    stepByStepInstructions: List[str] = []
    filesToModify: List[str] = []
    configurationChanges: Dict[str, Any] = {}
    manualActions: List[str] = []

# New Pipeline Stage Models for Enhanced Analysis
class PipelineStage(BaseModel):
    """Represents a Jenkins pipeline stage from /wfapi/describe"""
    id: str
    name: str
    status: str  # SUCCESS, FAILED, SKIPPED, etc.
    startTimeMillis: int
    durationMillis: int
    error: Optional[Dict[str, Any]] = None
    href: str
    execNode: Optional[str] = ""

class PipelineSubStage(BaseModel):
    """Represents a sub-stage/flow node within a pipeline stage"""
    id: str
    name: str
    status: str
    startTimeMillis: int
    durationMillis: int
    log_href: Optional[str] = None
    console_href: Optional[str] = None
    parameterDescription: Optional[str] = None
    parentNodes: List[str] = []

class PipelineDescription(BaseModel):
    """Complete pipeline description from Jenkins /wfapi/describe"""
    id: str
    name: str
    status: str
    startTimeMillis: int
    endTimeMillis: int
    durationMillis: int
    stages: List[PipelineStage]

class StageFailureContext(BaseModel):
    """Enhanced context for stage-specific failure analysis"""
    stage: PipelineStage
    sub_stages: List[PipelineSubStage]
    failed_sub_stages: List[PipelineSubStage]
    combined_logs: str
    is_fake_failure: bool = False
    log_size: int = 0

class StageLogs(BaseModel):
    """Logs from a specific pipeline stage/sub-stage"""
    stage_id: str
    sub_stage_ids: List[str]
    combined_log_text: str
    individual_logs: Dict[str, str] = {}  # sub_stage_id -> log_text



