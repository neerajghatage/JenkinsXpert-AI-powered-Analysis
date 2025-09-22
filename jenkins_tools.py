import requests
import xml.etree.ElementTree as ET
from config import config, logger
from models import *

# Import Azure OpenAI
try:
    from openai import AzureOpenAI
    AZURE_OPENAI_AVAILABLE = True
except ImportError:
    AZURE_OPENAI_AVAILABLE = False
    logger.warning("Azure OpenAI library not installed - AI analysis will be disabled")

class JenkinsAPI:
    def __init__(self):
        self.base_url = config.JENKINS_URL.rstrip('/')  # Remove trailing slash
        self.username = config.JENKINS_USERNAME
        self.token = config.JENKINS_API_TOKEN
        # Only use auth if both username and token are provided
        self.auth = (self.username, self.token) if self.username and self.token else None
    
    def create_url_from_path(self, jenkins_path: str, endpoint_type: str = "consoleText") -> str:
        try:
            # Remove trailing slash if present
            jenkins_path = jenkins_path.rstrip('/')
            
            # Extract base URL and path from the provided URL
            if '://' in jenkins_path:
                # Full URL provided
                if '/job/' in jenkins_path:
                    # Extract the job path part
                    job_path_start = jenkins_path.find('/job/')
                    base_jenkins_url = jenkins_path[:job_path_start]
                    job_path = jenkins_path[job_path_start:]
                else:
                    # No job path, treat as base URL
                    return f"{jenkins_path}/{endpoint_type}"
            else:
                # Relative path provided, use configured base URL
                base_jenkins_url = self.base_url
                job_path = jenkins_path if jenkins_path.startswith('/') else f"/{jenkins_path}"
            
            # Construct the final URL
            if endpoint_type == "consoleText":
                return f"{base_jenkins_url}{job_path}/consoleText"
            elif endpoint_type == "api/json":
                return f"{base_jenkins_url}{job_path}/api/json"
            elif endpoint_type == "config.xml":
                # For config.xml, we need to remove the build number from the path
                if job_path.count('/') >= 2:  # At least /job/name/buildnumber
                    path_parts = job_path.split('/')
                    if path_parts[-1].isdigit():  # Last part is build number
                        config_path = '/'.join(path_parts[:-1])  # Remove build number
                        return f"{base_jenkins_url}{config_path}/config.xml"
                return f"{base_jenkins_url}{job_path}/config.xml"
            else:
                return f"{base_jenkins_url}{job_path}/{endpoint_type}"
                
        except Exception as e:
            logger.error("Failed to create URL from path", jenkins_path=jenkins_path, endpoint_type=endpoint_type, error=str(e))
            # Fallback to original path + endpoint
            return f"{jenkins_path.rstrip('/')}/{endpoint_type}"
        
    def fetch_job_configuration_from_url(self, jenkins_url: str) -> JobConfiguration:
        """Retrieves complete Jenkins job configuration from a full Jenkins URL."""
        try:
            config_url = self.create_url_from_path(jenkins_url, "config.xml")
            response = requests.get(config_url, auth=self.auth)
            response.raise_for_status()
            
            # Parse XML configuration
            root = ET.fromstring(response.text)
            
            # Extract repository configuration
            repository = None
            scm_git = root.find(".//scm[@class='hudson.plugins.git.GitSCM']")
            if scm_git is not None:
                # Extract repository URL
                url_elem = scm_git.find(".//url")
                repo_url = url_elem.text if url_elem is not None else ""
                
                # Extract credentials
                creds_elem = scm_git.find(".//credentialsId")
                credentials_id = creds_elem.text if creds_elem is not None else None
                
                # Extract branches
                branches = []
                branch_elems = scm_git.findall(".//hudson.plugins.git.BranchSpec/name")
                for branch_elem in branch_elems:
                    if branch_elem.text:
                        branches.append(branch_elem.text)
                
                if repo_url:
                    repository = RepositoryConfig(
                        url=repo_url,
                        credentialsId=credentials_id,
                        branches=branches
                    )
            
            # Extract parameter definitions
            parameter_definitions = []
            param_props = root.find(".//hudson.model.ParametersDefinitionProperty")
            if param_props is not None:
                for param_def in param_props.findall(".//parameterDefinitions/*"):
                    name_elem = param_def.find("name")
                    desc_elem = param_def.find("description")
                    default_elem = param_def.find("defaultValue")
                    
                    if name_elem is not None and name_elem.text:
                        parameter_definitions.append(ParameterDefinition(
                            name=name_elem.text,
                            type=param_def.tag.split('.')[-1] if '.' in param_def.tag else param_def.tag,
                            defaultValue=default_elem.text if default_elem is not None else None,
                            description=desc_elem.text if desc_elem is not None else None
                        ))
            
            # Extract trigger configuration
            triggers = TriggerConfig()
            trigger_props = root.find(".//triggers")
            if trigger_props is not None:
                # GitHub hook trigger
                github_trigger = trigger_props.find(".//com.cloudbees.jenkins.GitHubPushTrigger")
                triggers.githubHook = github_trigger is not None
                
                # Poll SCM
                poll_trigger = trigger_props.find(".//hudson.triggers.SCMTrigger")
                if poll_trigger is not None:
                    spec_elem = poll_trigger.find("spec")
                    triggers.pollSCM = spec_elem.text if spec_elem is not None else None
                
                # Build periodically
                timer_trigger = trigger_props.find(".//hudson.triggers.TimerTrigger")
                if timer_trigger is not None:
                    spec_elem = timer_trigger.find("spec")
                    triggers.buildPeriodically = spec_elem.text if spec_elem is not None else None
            
            # Extract environment configuration
            environment = EnvironmentConfig()
            
            # Delete workspace
            delete_ws = root.find(".//hudson.plugins.ws__cleanup.PreBuildCleanup")
            environment.deleteWorkspace = delete_ws is not None
            
            # Build wrappers for other environment settings
            wrappers = root.find(".//buildWrappers")
            if wrappers is not None:
                # Secret bindings
                secret_wrapper = wrappers.find(".//org.jenkinsci.plugins.credentialsbinding.impl.SecretBuildWrapper")
                environment.useSecrets = secret_wrapper is not None
                
                # Timestamps
                timestamp_wrapper = wrappers.find(".//hudson.plugins.timestamper.TimestamperBuildWrapper")
                environment.addTimestamps = timestamp_wrapper is not None
                
                # Timeout
                timeout_wrapper = wrappers.find(".//hudson.plugins.build__timeout.BuildTimeoutWrapper")
                if timeout_wrapper is not None:
                    timeout_elem = timeout_wrapper.find(".//timeoutMinutes")
                    if timeout_elem is not None and timeout_elem.text:
                        environment.timeoutMinutes = int(timeout_elem.text)
            
            # Extract build steps and script text
            build_steps = []
            script_text = ""
            
            # Check for Pipeline job script first
            script_element = root.find(".//script")
            if script_element is not None and script_element.text:
                script_text = script_element.text
            else:
                # Extract Freestyle job build steps
                # Shell build steps
                for command_elem in root.findall(".//hudson.tasks.Shell/command"):
                    if command_elem.text:
                        step_content = f"# Shell Build Step\n{command_elem.text}"
                        build_steps.append(step_content)
                
                # Batch build steps (Windows)
                for command_elem in root.findall(".//hudson.tasks.BatchFile/command"):
                    if command_elem.text:
                        step_content = f"# Batch Build Step\n{command_elem.text}"
                        build_steps.append(step_content)
                
                # Other build steps
                builders = root.find(".//builders")
                if builders is not None:
                    for builder in builders:
                        builder_class = builder.get('class', builder.tag)
                        if 'command' in [child.tag for child in builder]:
                            cmd_elem = builder.find('command')
                            if cmd_elem is not None and cmd_elem.text:
                                step_content = f"# {builder_class}\n{cmd_elem.text}"
                                build_steps.append(step_content)
                
                # Combine build steps into script text
                if build_steps:
                    script_text = "\n\n".join(build_steps)
            
            # Extract general settings
            description_elem = root.find(".//description")
            description = description_elem.text if description_elem is not None else None
            
            concurrent_elem = root.find(".//concurrentBuild")
            concurrent = concurrent_elem is not None and concurrent_elem.text == "true"
            
            # Check for throttle
            throttle_elem = root.find(".//hudson.plugins.throttleconcurrents.ThrottleJobProperty")
            throttle = throttle_elem is not None
            
            return JobConfiguration(
                repository=repository,
                parameterDefinitions=parameter_definitions,
                triggers=triggers,
                environment=environment,
                buildSteps=build_steps,
                scriptText=script_text,
                description=description,
                concurrent=concurrent,
                throttle=throttle
            )
            
        except Exception as e:
            logger.error("Failed to fetch job configuration", jenkins_url=jenkins_url, error=str(e))
            raise
    
    def fetch_build_parameters_from_url(self, jenkins_url: str) -> BuildParameters:
        """Retrieves all build parameters used in a specific build via Jenkins API from URL."""
        try:
            api_url = self.create_url_from_path(jenkins_url, "api/json")
            response = requests.get(api_url, auth=self.auth)
            response.raise_for_status()
            
            build_data = response.json()
            parameters = {}
            
            if 'actions' in build_data:
                for action in build_data['actions']:
                    if action.get('_class') == 'hudson.model.ParametersAction':
                        for param in action.get('parameters', []):
                            parameters[param.get('name')] = param.get('value')
            
            return BuildParameters(parameters=parameters)
        except Exception as e:
            logger.error("Failed to fetch build parameters", jenkins_url=jenkins_url, error=str(e))
            raise
    
    def fetch_build_logs_from_url(self, jenkins_url: str) -> BuildLogs:
        """Fetches the complete console log from Jenkins using the full URL."""
        try:
            console_url = self.create_url_from_path(jenkins_url, "consoleText")
            response = requests.get(console_url, auth=self.auth)
            response.raise_for_status()
            
            return BuildLogs(logText=response.text)
        except Exception as e:
            logger.error("Failed to fetch build logs", jenkins_url=jenkins_url, error=str(e))
            raise

    def fetch_pipeline_description(self, jenkins_url: str) -> PipelineDescription:
        """Fetches pipeline description with all stages from Jenkins /wfapi/describe endpoint."""
        try:
            # Create the wfapi/describe URL
            wfapi_url = f"{jenkins_url.rstrip('/')}/wfapi/describe"
            
            logger.info("Fetching pipeline description", wfapi_url=wfapi_url)
            response = requests.get(wfapi_url, auth=self.auth)
            response.raise_for_status()
            
            pipeline_data = response.json()
            
            # Parse stages
            stages = []
            for stage_data in pipeline_data.get('stages', []):
                # Extract the href for stage details
                stage_href = stage_data.get('_links', {}).get('self', {}).get('href', '')
                
                stage = PipelineStage(
                    id=stage_data.get('id', ''),
                    name=stage_data.get('name', ''),
                    status=stage_data.get('status', ''),
                    startTimeMillis=stage_data.get('startTimeMillis', 0),
                    durationMillis=stage_data.get('durationMillis', 0),
                    error=stage_data.get('error'),
                    href=stage_href,
                    execNode=stage_data.get('execNode', '')
                )
                stages.append(stage)
            
            return PipelineDescription(
                id=pipeline_data.get('id', ''),
                name=pipeline_data.get('name', ''),
                status=pipeline_data.get('status', ''),
                startTimeMillis=pipeline_data.get('startTimeMillis', 0),
                endTimeMillis=pipeline_data.get('endTimeMillis', 0),
                durationMillis=pipeline_data.get('durationMillis', 0),
                stages=stages
            )
            
        except Exception as e:
            logger.error("Failed to fetch pipeline description", jenkins_url=jenkins_url, error=str(e))
            raise

    def fetch_stage_sub_stages(self, jenkins_url: str, stage_href: str) -> List[PipelineSubStage]:
        """Fetches sub-stages for a specific pipeline stage."""
        try:
            # Extract base URL from jenkins_url
            if '://' in jenkins_url:
                base_url = jenkins_url.split('://', 1)[1]
                base_url = jenkins_url.split('://', 1)[0] + '://' + base_url.split('/', 1)[0]
            else:
                base_url = self.base_url
            
            # Create full URL for stage details
            stage_url = f"{base_url.rstrip('/')}{stage_href}"

            print(stage_url)
            
            logger.info("Fetching stage sub-stages", stage_url=stage_url)
            response = requests.get(stage_url, auth=self.auth)
            response.raise_for_status()
            
            stage_data = response.json()
            
            # Parse stageFlowNodes (sub-stages)
            sub_stages = []
            for node_data in stage_data.get('stageFlowNodes', []):
                # Extract log URLs
                links = node_data.get('_links', {})
                log_href = links.get('log', {}).get('href', '')
                console_href = links.get('console', {}).get('href', '')
                
                sub_stage = PipelineSubStage(
                    id=node_data.get('id', ''),
                    name=node_data.get('name', ''),
                    status=node_data.get('status', ''),
                    startTimeMillis=node_data.get('startTimeMillis', 0),
                    durationMillis=node_data.get('durationMillis', 0),
                    log_href=log_href,
                    console_href=console_href,
                    parameterDescription=node_data.get('parameterDescription'),
                    parentNodes=node_data.get('parentNodes', [])
                )
                sub_stages.append(sub_stage)
            
            return sub_stages
            
        except Exception as e:
            logger.error("Failed to fetch stage sub-stages", stage_href=stage_href, error=str(e))
            raise

    def fetch_sub_stage_logs(self, jenkins_url: str, sub_stage_id: str) -> str:
        """Fetches logs for a specific sub-stage using pipeline-console/log endpoint."""
        try:
            # Extract base URL from jenkins_url
            if '://' in jenkins_url:
                base_url = jenkins_url.split('://', 1)[1]
                base_url = jenkins_url.split('://', 1)[0] + '://' + base_url.split('/', 1)[0]
            else:
                base_url = self.base_url
            
            # Extract the job path from jenkins_url
            job_path = ""
            if '/job/' in jenkins_url:
                job_path = jenkins_url[jenkins_url.find('/job/'):]
                # Remove trailing build number if present
                if job_path.split('/')[-1].isdigit():
                    job_path = '/'.join(job_path.split('/')[:-1])
            
            # Build number from URL
            build_number = ""
            if jenkins_url.split('/')[-1].isdigit():
                build_number = jenkins_url.split('/')[-1]
            
            # Create pipeline console log URL
            log_url = f"{base_url.rstrip('/')}{job_path}/{build_number}/pipeline-console/log?nodeId={sub_stage_id}"
            
            logger.info("Fetching sub-stage logs", log_url=log_url, sub_stage_id=sub_stage_id)
            response = requests.get(log_url, auth=self.auth)
            response.raise_for_status()
            
            return response.text
            
        except Exception as e:
            logger.error("Failed to fetch sub-stage logs", sub_stage_id=sub_stage_id, error=str(e))
            return f"Error fetching logs for sub-stage {sub_stage_id}: {str(e)}"

    def detect_fake_failures(self, stage: PipelineStage, sub_stages: List[PipelineSubStage], actual_failure_found: bool = False) -> bool:
        """
        Determine if a FAILED stage is a real failure or a cascade failure.
        
        Jenkins often marks subsequent stages as FAILED when they're actually
        SKIPPED due to earlier failures. This method uses heuristics to detect
        the difference.
        """
        try:
            # Only check stages that are marked as FAILED, ABORTED, or similar failure states
            if stage.status not in ["FAILED", "ABORTED"]:
                return False
            
            duration = stage.durationMillis or 0
            error_info = stage.error or {}
            error_message = error_info.get('message', '') if error_info else ''
            
            # Check explicit skip patterns first (highest confidence fake failure detection)
            if stage.error:
                error_msg = error_message.lower()
                skip_patterns = [
                    'skipped',
                    'when condition',
                    'stage skipped due to when conditional',
                    'conditional skip',
                    'when clause evaluated to false',
                    'stage condition returned false',
                    'skipped due to earlier failure'
                ]
                
                if any(pattern in error_msg for pattern in skip_patterns):
                    logger.info("Detected fake failure - explicit skip pattern", 
                               stage_id=stage.id, 
                               stage_name=stage.name,
                               error_pattern=error_msg[:100])
                    return True
            
            # If we've already found a real failure, subsequent "failures" with
            # short durations are likely cascade failures
            if actual_failure_found:
                # Very short duration suggests the stage was terminated quickly
                # rather than actually executed and failed
                if duration < 2000:  # Less than 2 seconds
                    logger.info("Detected cascade failure - very short duration", 
                               stage_id=stage.id, 
                               stage_name=stage.name,
                               duration_ms=duration)
                    return True
                
                # Generic error messages often indicate cascade failures
                generic_errors = [
                    'script returned exit code 1',
                    'build step failed',
                    'stage failed',
                    'pipeline aborted',
                    'build was aborted',
                    'execution was cancelled'
                ]
                
                error_message_lower = error_message.lower()
                if any(generic_error in error_message_lower for generic_error in generic_errors):
                    # If it's a generic error AND short duration, likely cascade
                    if duration < 5000:  # Less than 5 seconds
                        logger.info("Detected cascade failure - generic error with short duration", 
                                   stage_id=stage.id, 
                                   stage_name=stage.name,
                                   duration_ms=duration,
                                   error_snippet=error_message[:50])
                        return True
            
            # Long duration suggests real work was done before failure
            if duration > 10000:  # More than 10 seconds of actual work
                logger.debug("Likely real failure - significant duration", 
                           stage_id=stage.id, 
                           stage_name=stage.name,
                           duration_ms=duration)
                return False
            
            # Specific error types that indicate real failures
            real_failure_types = [
                'compilation',
                'test failure',
                'build failure',
                'deployment failure',
                'timeout',
                'npm error',
                'maven build failure',
                'gradle build failed',
                'docker build failed',
                'junit',
                'assertion',
                'syntax error',
                'permission denied',
                'file not found',
                'connection refused'
            ]
            
            if error_message:
                error_message_lower = error_message.lower()
                if any(real_error in error_message_lower for real_error in real_failure_types):
                    logger.debug("Likely real failure - specific error type", 
                               stage_id=stage.id, 
                               stage_name=stage.name,
                               error_type=error_message[:100])
                    return False
            
            # Check if all sub-stages were skipped or have no meaningful execution
            if sub_stages:
                meaningful_executions = [
                    sub for sub in sub_stages 
                    if sub.status not in ['SKIPPED', 'NOT_EXECUTED', 'NOT_BUILT'] and sub.durationMillis > 100
                ]
                
                if len(meaningful_executions) == 0:
                    logger.info("Detected fake failure - no meaningful sub-stage executions", 
                               stage_id=stage.id, 
                               stage_name=stage.name,
                               total_sub_stages=len(sub_stages))
                    return True
                
                # If most sub-stages were skipped, likely a fake failure
                skip_ratio = (len(sub_stages) - len(meaningful_executions)) / len(sub_stages)
                if skip_ratio > 0.8:  # More than 80% of sub-stages were skipped
                    logger.info("Detected fake failure - high skip ratio", 
                               stage_id=stage.id, 
                               stage_name=stage.name,
                               skip_ratio=f"{skip_ratio:.2f}",
                               meaningful_executions=len(meaningful_executions))
                    return True
            
            # For ABORTED status with short duration, likely cascade
            if stage.status == "ABORTED" and duration < 3000:
                logger.info("Detected fake failure - aborted with short duration", 
                           stage_id=stage.id, 
                           stage_name=stage.name,
                           duration_ms=duration)
                return True
            
            # Default: assume it's a real failure if we can't prove otherwise
            logger.debug("Assuming real failure - no fake failure patterns detected", 
                       stage_id=stage.id, 
                       stage_name=stage.name,
                       duration_ms=duration,
                       status=stage.status)
            return False
            
        except Exception as e:
            logger.error("Error detecting fake failures", 
                       stage_id=stage.id, 
                       stage_name=getattr(stage, 'name', 'unknown'),
                       error=str(e))
            return False

    def analyze_pipeline_stages_from_url(self, jenkins_url: str) -> List[StageFailureContext]:
        """
        Enhanced pipeline analysis: fetches pipeline stages, identifies real failures,
        and collects targeted logs for failed stages only.
        """
        try:
            logger.info("Starting enhanced pipeline stage analysis", jenkins_url=jenkins_url)
            
            # 1. Fetch pipeline description with all stages
            pipeline_desc = self.fetch_pipeline_description(jenkins_url)
            logger.info("Retrieved pipeline stages", 
                       total_stages=len(pipeline_desc.stages),
                       pipeline_status=pipeline_desc.status)
            
            # 2. Early success detection - check if all stages are passing
            failed_or_aborted_stages = [stage for stage in pipeline_desc.stages if stage.status in ["FAILED", "ABORTED"]]
            
            if not failed_or_aborted_stages:
                # All stages are passing (SUCCESS, IN_PROGRESS, etc.)
                logger.info("Build is successful - no failing stages detected", 
                           total_stages=len(pipeline_desc.stages),
                           all_stages_status=[f"{stage.name}:{stage.status}" for stage in pipeline_desc.stages[:3]])
                
                # Return empty list to indicate successful build (no failures to analyze)
                return []
            
            logger.info("Failed/Aborted stages detected", 
                       failed_aborted_count=len(failed_or_aborted_stages),
                       failed_stages=[f"{stage.name}:{stage.status}" for stage in failed_or_aborted_stages[:3]])
            
            stage_failure_contexts = []
            actual_failure_found = False
            
            # 3. Process each stage to identify real failures
            for stage in pipeline_desc.stages:
                logger.info("Processing stage", 
                           stage_id=stage.id, 
                           stage_name=stage.name, 
                           status=stage.status)
                
                # Skip non-failed stages
                if stage.status not in ["FAILED", "ABORTED"]:
                    continue
                
                # Skip ABORTED stages immediately - they're typically cancelled due to earlier failures
                if stage.status == "ABORTED":
                    logger.info("Skipping aborted stage - cancelled due to earlier failures", 
                               stage_id=stage.id, 
                               stage_name=stage.name)
                    continue
                
                # Pre-check for obvious fake failures using stage-level data only (no API calls)
                duration = stage.durationMillis or 0
                
                # If we've already found a real failure, check for cascade failures
                if actual_failure_found and duration < 2000:  # Less than 2 seconds
                    logger.info("Detected cascade failure - very short duration", 
                               stage_id=stage.id, 
                               stage_name=stage.name,
                               duration_ms=duration)
                    continue
                
                # Check explicit skip patterns in stage error (no API calls needed)
                if stage.error:
                    error_message = stage.error.get('message', '').lower()
                    skip_patterns = [
                        'skipped', 'when condition', 'stage skipped due to when conditional',
                        'conditional skip', 'when clause evaluated to false',
                        'stage condition returned false', 'skipped due to earlier failure'
                    ]
                    
                    if any(pattern in error_message for pattern in skip_patterns):
                        logger.info("Detected fake failure - explicit skip pattern", 
                                   stage_id=stage.id, 
                                   stage_name=stage.name,
                                   error_pattern=error_message[:100])
                        continue
                
                try:
                    # 3. Only fetch sub-stages if we haven't identified this as fake
                    sub_stages = self.fetch_stage_sub_stages(jenkins_url, stage.href)
                    logger.info("Retrieved sub-stages", 
                               stage_id=stage.id, 
                               sub_stages_count=len(sub_stages))
                    
                    # 4. Final fake failure check with sub-stage data
                    is_fake = self.detect_fake_failures(stage, sub_stages, actual_failure_found)
                    if is_fake:
                        logger.info("Skipping fake failure", stage_id=stage.id, stage_name=stage.name)
                        continue
                    
                    # Mark that we've found a real failure
                    actual_failure_found = True
                    
                    # 5. Identify actually failed sub-stages (filter out fake failures)
                    failed_sub_stages = []
                    for sub in sub_stages:
                        if sub.status not in ["FAILED", "ABORTED"]:
                            continue
                        
                        # Skip ABORTED sub-stages (cancelled due to earlier failures)
                        if sub.status == "ABORTED":
                            logger.info("Skipping aborted sub-stage", 
                                       sub_stage_id=sub.id, 
                                       sub_stage_name=sub.name)
                            continue
                        
                        # Skip sub-stages with zero or very short duration (likely fake)
                        if (sub.durationMillis or 0) < 500:  # Less than 0.5 seconds
                            logger.info("Skipping fake sub-stage - very short duration", 
                                       sub_stage_id=sub.id, 
                                       sub_stage_name=sub.name,
                                       duration_ms=sub.durationMillis)
                            continue
                        
                        failed_sub_stages.append(sub)
                    
                    logger.info("Identified failed sub-stages", 
                               stage_id=stage.id, 
                               failed_count=len(failed_sub_stages))
                    
                    # 6. Collect logs only from genuinely failed sub-stages
                    combined_logs = ""
                    individual_logs = {}
                    
                    for failed_sub in failed_sub_stages:
                        try:
                            logger.info("Fetching sub-stage logs", 
                                       sub_stage_id=failed_sub.id, 
                                       log_url=f"{jenkins_url.rstrip('/')}/pipeline-console/log?nodeId={failed_sub.id}")
                            
                            sub_log = self.fetch_sub_stage_logs(jenkins_url, failed_sub.id)
                            individual_logs[failed_sub.id] = sub_log
                            
                            # Add to combined logs with clear separation
                            combined_logs += f"\n\n=== STAGE: {stage.name} | SUB-STAGE: {failed_sub.name} (ID: {failed_sub.id}) ===\n"
                            combined_logs += sub_log
                            combined_logs += f"\n=== END SUB-STAGE {failed_sub.id} ===\n"
                            
                        except Exception as e:
                            logger.error("Failed to fetch sub-stage logs", 
                                       sub_stage_id=failed_sub.id, 
                                       error=str(e))
                    
                    # 7. Create failure context
                    failure_context = StageFailureContext(
                        stage=stage,
                        sub_stages=sub_stages,
                        failed_sub_stages=failed_sub_stages,
                        combined_logs=combined_logs,
                        is_fake_failure=False,
                        log_size=len(combined_logs)
                    )
                    
                    stage_failure_contexts.append(failure_context)
                    
                    logger.info("Created stage failure context", 
                               stage_id=stage.id, 
                               log_size=len(combined_logs),
                               failed_sub_stages=len(failed_sub_stages))
                    
                except Exception as e:
                    logger.error("Error processing stage", stage_id=stage.id, error=str(e))
                    continue
            
            logger.info("Pipeline stage analysis completed", 
                       total_failure_contexts=len(stage_failure_contexts))
            
            return stage_failure_contexts
            
        except Exception as e:
            logger.error("Pipeline stage analysis failed", jenkins_url=jenkins_url, error=str(e))
            raise


class FailureParser:
    def __init__(self):
        self.error_keywords = [
            'ERROR', 'FAILED', 'Exception', 'Error', 'BUILD FAILED', 'failed', 'error'
        ]
    
    def parse_failure_events(self, log_text: str) -> ParsedFailures:
        """Enhanced parsing for logs (1-10000 lines) - intelligently finds most relevant errors with precise context."""
        try:
            failures = []
            lines = log_text.split('\n')
            total_lines = len(lines)
            
            # Enhanced error detection strategy based on log size with better handling for very large logs
            if total_lines > 5000:
                # VERY LARGE LOGS (5000+ lines): Ultra-focused strategy
                
                # Pass 1: Critical build failures in last 200 lines (most recent)
                critical_keywords = ['BUILD FAILED', 'FATAL ERROR', 'CRITICAL', 'COMPILATION FAILED', 'TEST FAILED', 'DEPLOYMENT FAILED']
                for i in range(total_lines - 1, max(0, total_lines - 200), -1):
                    line = lines[i].upper()
                    for keyword in critical_keywords:
                        if keyword in line:
                            # Large context for critical errors
                            context_start = max(0, i - 100)
                            context_end = min(total_lines, i + 100)
                            
                            failure = FailureEvent(
                                line=i + 1,
                                message=lines[i].strip(),
                                type="CRITICAL_BUILD_FAILURE",
                                context={
                                    'surrounding_lines': lines[context_start:context_end],
                                    'line_number': i + 1,
                                    'context_start': context_start + 1,
                                    'context_end': context_end,
                                    'total_log_lines': total_lines,
                                    'error_context': '\n'.join(lines[context_start:context_end]),
                                    'search_strategy': 'very_large_critical_recent',
                                    'log_size_category': 'very_large'
                                }
                            )
                            failures.append(failure)
                            return ParsedFailures(failures=failures)
                
                # Pass 2: Error patterns in strategic sections (beginning, middle, end)
                strategic_sections = [
                    (0, min(300, total_lines), 'very_large_early'),
                    (max(0, total_lines//2 - 150), min(total_lines, total_lines//2 + 150), 'very_large_middle'),
                    (max(0, total_lines - 500), total_lines, 'very_large_final')
                ]
                
                for start, end, strategy in strategic_sections:
                    for i in range(start, end):
                        line = lines[i].upper()
                        if any(keyword in line for keyword in ['ERROR', 'FAILED', 'Exception', 'FAILURE']):
                            if not any(skip in line for skip in ['INFO', 'DEBUG', 'WARNING']):
                                context_start = max(0, i - 30)
                                context_end = min(total_lines, i + 70)
                                
                                failure = FailureEvent(
                                    line=i + 1,
                                    message=lines[i].strip(),
                                    type="STRATEGIC_SECTION_ERROR",
                                    context={
                                        'surrounding_lines': lines[context_start:context_end],
                                        'line_number': i + 1,
                                        'context_start': context_start + 1,
                                        'context_end': context_end,
                                        'total_log_lines': total_lines,
                                        'error_context': '\n'.join(lines[context_start:context_end]),
                                        'search_strategy': strategy,
                                        'log_size_category': 'very_large'
                                    }
                                )
                                failures.append(failure)
                                return ParsedFailures(failures=failures)
                
            elif total_lines > 2000:
                # LARGE LOGS (2000-5000 lines): Multi-pass strategy for precision
                
                # Pass 1: Critical errors in last 300 lines (most recent failures)
                critical_keywords = ['BUILD FAILED', 'FATAL ERROR', 'CRITICAL', 'COMPILATION FAILED', 'TEST FAILED']
                for i in range(total_lines - 1, max(0, total_lines - 300), -1):
                    line = lines[i].upper()
                    for keyword in critical_keywords:
                        if keyword in line:
                            # Large context for critical errors (50 lines before + 100 after)
                            context_start = max(0, i - 50)
                            context_end = min(total_lines, i + 100)
                            
                            failure = FailureEvent(
                                line=i + 1,
                                message=lines[i].strip(),
                                type="CRITICAL_FAILURE",
                                context={
                                    'surrounding_lines': lines[context_start:context_end],
                                    'line_number': i + 1,
                                    'context_start': context_start + 1,
                                    'context_end': context_end,
                                    'total_log_lines': total_lines,
                                    'error_context': '\n'.join(lines[context_start:context_end]),
                                    'search_strategy': 'large_critical_recent',
                                    'log_size_category': 'large'
                                }
                            )
                            failures.append(failure)
                            return ParsedFailures(failures=failures)
                
                # Pass 2: First error occurrence (root cause in first 500 lines)
                error_keywords = ['ERROR', 'FAILED', 'Exception', 'FAILURE']
                for i in range(min(500, total_lines)):
                    line = lines[i].upper()
                    for keyword in error_keywords:
                        if keyword in line and not any(skip in line for skip in ['INFO', 'DEBUG', 'WARNING']):
                            # Extended context for root cause errors
                            context_start = max(0, i - 20)
                            context_end = min(total_lines, i + 80)
                            
                            failure = FailureEvent(
                                line=i + 1,
                                message=lines[i].strip(),
                                type="ROOT_CAUSE_ERROR",
                                context={
                                    'surrounding_lines': lines[context_start:context_end],
                                    'line_number': i + 1,
                                    'context_start': context_start + 1,
                                    'context_end': context_end,
                                    'total_log_lines': total_lines,
                                    'error_context': '\n'.join(lines[context_start:context_end]),
                                    'search_strategy': 'large_root_cause_early',
                                    'log_size_category': 'large'
                                }
                            )
                            failures.append(failure)
                            return ParsedFailures(failures=failures)
                
                # Pass 3: Final build status (last 50 lines if no specific errors found)
                final_context = lines[-50:]
                failure = FailureEvent(
                    line=total_lines - 25,
                    message="Build completion status check",
                    type="BUILD_STATUS",
                    context={
                        'surrounding_lines': final_context,
                        'line_number': total_lines - 25,
                        'context_start': total_lines - 50,
                        'context_end': total_lines,
                        'total_log_lines': total_lines,
                        'error_context': '\n'.join(final_context),
                        'search_strategy': 'final_status'
                    }
                )
                failures.append(failure)
                
            elif total_lines > 500:
                # MEDIUM LOGS (500-2000 lines): Focused search
                
                # Look for errors in last 200 lines first
                for i in range(total_lines - 1, max(0, total_lines - 200), -1):
                    line = lines[i].upper()
                    if any(keyword in line for keyword in ['ERROR', 'FAILED', 'Exception', 'BUILD FAILED']):
                        context_start = max(0, i - 15)
                        context_end = min(total_lines, i + 35)
                        
                        failure = FailureEvent(
                            line=i + 1,
                            message=lines[i].strip(),
                            type="RECENT_ERROR",
                            context={
                                'surrounding_lines': lines[context_start:context_end],
                                'line_number': i + 1,
                                'context_start': context_start + 1,
                                'context_end': context_end,
                                'total_log_lines': total_lines,
                                'error_context': '\n'.join(lines[context_start:context_end]),
                                'search_strategy': 'medium_recent'
                            }
                        )
                        failures.append(failure)
                        return ParsedFailures(failures=failures)
                
                # If no recent errors, check first 300 lines
                for i in range(min(300, total_lines)):
                    line = lines[i].upper()
                    if any(keyword in line for keyword in ['ERROR', 'FAILED', 'Exception']):
                        context_start = max(0, i - 10)
                        context_end = min(total_lines, i + 25)
                        
                        failure = FailureEvent(
                            line=i + 1,
                            message=lines[i].strip(),
                            type="EARLY_ERROR",
                            context={
                                'surrounding_lines': lines[context_start:context_end],
                                'line_number': i + 1,
                                'context_start': context_start + 1,
                                'context_end': context_end,
                                'total_log_lines': total_lines,
                                'error_context': '\n'.join(lines[context_start:context_end]),
                                'search_strategy': 'medium_early'
                            }
                        )
                        failures.append(failure)
                        return ParsedFailures(failures=failures)
                        
            else:
                # SMALL LOGS (1-500 lines): Comprehensive scan
                error_keywords = ['ERROR', 'FAILED', 'Exception', 'Error', 'BUILD FAILED', 'failed', 'error']
                
                for i, line in enumerate(lines):
                    line_upper = line.upper()
                    for keyword in error_keywords:
                        if keyword.upper() in line_upper:
                            # Generous context for small logs
                            context_start = max(0, i - 10)
                            context_end = min(total_lines, i + 20)
                            
                            failure = FailureEvent(
                                line=i + 1,
                                message=line.strip(),
                                type="SMALL_LOG_ERROR",
                                context={
                                    'surrounding_lines': lines[context_start:context_end],
                                    'line_number': i + 1,
                                    'context_start': context_start + 1,
                                    'context_end': context_end,
                                    'total_log_lines': total_lines,
                                    'error_context': '\n'.join(lines[context_start:context_end]),
                                    'search_strategy': 'small_comprehensive'
                                }
                            )
                            failures.append(failure)
                            break  # Only first error for small logs
                
                # If errors found, return first one for small logs
                if failures:
                    return ParsedFailures(failures=failures[:1])
                    
                # If no errors found in small log, return entire log as context
                failure = FailureEvent(
                    line=total_lines // 2,
                    message="No explicit errors found - full log analysis",
                    type="NO_ERRORS_FOUND",
                    context={
                        'surrounding_lines': lines,
                        'line_number': total_lines // 2,
                        'context_start': 1,
                        'context_end': total_lines,
                        'total_log_lines': total_lines,
                        'error_context': log_text if total_lines <= 200 else '\n'.join(lines[:100] + ['...'] + lines[-100:]),
                        'search_strategy': 'full_log_fallback'
                    }
                )
                failures.append(failure)
            
            return ParsedFailures(failures=failures)
            
        except Exception as e:
            logger.error("Failed to parse failure events", error=str(e))
            raise

class FailureAnalyzer:
    def __init__(self):
        # Initialize Azure OpenAI client with enhanced debugging
        self.openai_client = None
        print(f"AZURE_OPENAI_AVAILABLE: {AZURE_OPENAI_AVAILABLE}")
        print(f"API Key configured: {bool(config.AZURE_OPENAI_API_KEY and config.AZURE_OPENAI_API_KEY != '')}")
        print(f"API Key length: {len(config.AZURE_OPENAI_API_KEY) if config.AZURE_OPENAI_API_KEY else 0}")
        print(f"Endpoint configured: {config.AZURE_OPENAI_ENDPOINT}")
        print(f"Deployment: {config.AZURE_OPENAI_DEPLOYMENT}")
        
        if AZURE_OPENAI_AVAILABLE and config.AZURE_OPENAI_API_KEY and config.AZURE_OPENAI_API_KEY != "":
            try:
                # Workaround for container environment proxy parameter issue
                # Try multiple initialization approaches to handle container-specific issues
                
                # First attempt: Standard initialization
                try:
                    self.openai_client = AzureOpenAI(
                        api_key=config.AZURE_OPENAI_API_KEY,
                        api_version=config.AZURE_OPENAI_API_VERSION,
                        azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
                        timeout=30.0
                    )
                    print(" Standard Azure OpenAI initialization successful")
                except TypeError as e:
                    if "proxies" in str(e):
                        print(f" Standard init failed due to proxies issue: {e}")
                        # Second attempt: Try without timeout parameter
                        try:
                            self.openai_client = AzureOpenAI(
                                api_key=config.AZURE_OPENAI_API_KEY,
                                api_version=config.AZURE_OPENAI_API_VERSION,
                                azure_endpoint=config.AZURE_OPENAI_ENDPOINT
                            )
                            print(" Fallback Azure OpenAI initialization successful")
                        except Exception as e2:
                            print(f" Fallback init also failed: {e2}")
                            raise e2
                    else:
                        raise e
                
                if self.openai_client:
                    logger.info("Azure OpenAI client initialized successfully", deployment=config.AZURE_OPENAI_DEPLOYMENT)
                    print("Azure OpenAI client initialized successfully")
                    
            except Exception as e:
                logger.warning("Failed to initialize Azure OpenAI client", error=str(e))
                print(f"Azure OpenAI client initialization failed: {e}")
                self.openai_client = None
        else:
            logger.warning("Azure OpenAI API key not provided - AI analysis will be disabled")
            print("Azure OpenAI API key not provided - AI analysis will be disabled")
        
        self.jenkins_api = JenkinsAPI()
        
        # Initialize comprehensive error detection patterns
        self._init_error_patterns()
    
    def _init_error_patterns(self):
        """Initialize comprehensive error detection patterns for smart log filtering."""
        import re
        
        # Core error patterns - highest priority
        self.core_error_patterns = [
            r'(?i)\berror\b(?:\s*[:]\s*|\s+)',
            r'(?i)\bfailed\b(?:\s*[:]\s*|\s+)',
            r'(?i)\bfailure\b(?:\s*[:]\s*|\s+)',
            r'(?i)\bexception\b(?:\s*[:]\s*|\s+)',
            r'(?i)\bcrash\b(?:\s*[:]\s*|\s+)',
            r'(?i)\bpanic\b(?:\s*[:]\s*|\s+)',
        ]
        
        # Extended error indicators - medium priority
        self.extended_error_patterns = [
            r'(?i)\berrno\b',
            r'(?i)\berr\b(?:\s*[:]\s*|\s+)',
            r'(?i)\bfail\b(?:\s*[:]\s*|\s+)',
            r'(?i)\bbuild\s+failed\b',
            r'(?i)\bcompilation\s+failed\b',
            r'(?i)\btest\s+failed\b',
            r'(?i)\bdeployment\s+failed\b',
            r'(?i)\bcritical\b(?:\s*[:]\s*|\s+)',
            r'(?i)\bfatal\s+error\b',
            r'(?i)\baborted\b',
            r'(?i)\bunstable\b',
            r'(?i)\btimeout\b',
            r'(?i)\bkilled\b',
            r'(?i)\bsegmentation\s+fault\b',
            r'(?i)\baccess\s+violation\b',
            r'(?i)\bnull\s+pointer\b',
            r'(?i)\bassert\b(?:\s*[:]\s*|\s+)',
        ]
        
        # Exit code and status indicators
        self.exit_code_patterns = [
            r'(?i)exit\s+code\s*[:]\s*(?!0\b)\d+',  # Non-zero exit codes
            r'(?i)returned\s+exit\s+code\s+(?!0\b)\d+',
            r'(?i)process\s+exited\s+with\s+code\s+(?!0\b)\d+',
            r'(?i)status\s*[:]\s*failed',
            r'(?i)result\s*[:]\s*(?:failure|failed|error)',
        ]
        
        # File system and permission errors
        self.filesystem_patterns = [
            r'(?i)(?:file|directory)\s+not\s+found',
            r'(?i)no\s+such\s+file\s+or\s+directory',
            r'(?i)permission\s+denied',
            r'(?i)access\s+denied',
            r'(?i)cannot\s+(?:open|read|write|access)',
            r'(?i)unable\s+to\s+(?:open|read|write|access|find)',
            r'(?i)missing\s+(?:file|dependency)',
        ]
        
        # Network and connection errors
        self.network_patterns = [
            r'(?i)connection\s+(?:failed|refused|timeout|reset)',
            r'(?i)network\s+(?:error|timeout|unreachable)',
            r'(?i)dns\s+(?:error|resolution\s+failed)',
            r'(?i)ssl\s+(?:error|handshake\s+failed)',
            r'(?i)certificate\s+(?:error|invalid|expired)',
            r'(?i)authentication\s+failed',
        ]
        
        # Compilation and build errors
        self.compilation_patterns = [
            r'(?i)syntax\s+error',
            r'(?i)compile\s+error',
            r'(?i)compilation\s+(?:error|failed)',
            r'(?i)linker\s+error',
            r'(?i)undefined\s+(?:reference|symbol)',
            r'(?i)multiple\s+definition',
            r'(?i)cannot\s+find\s+(?:library|symbol|header)',
            r'(?i)missing\s+(?:dependency|library|package)',
            r'(?i)unresolved\s+(?:reference|dependency)',
            r'(?i)fatal\s+error',
            r'(?i)internal\s+compiler\s+error',
            r'(?i)build\s+(?:error|failure)',
        ]
        
        # Memory and resource errors
        self.memory_patterns = [
            r'(?i)out\s+of\s+memory',
            r'(?i)memory\s+allocation\s+(?:failed|error)',
            r'(?i)insufficient\s+(?:memory|disk\s+space)',
            r'(?i)resource\s+(?:exhausted|unavailable)',
            r'(?i)heap\s+(?:overflow|corruption)',
            r'(?i)stack\s+overflow',
        ]
        
        # Test and validation errors
        self.test_patterns = [
            r'(?i)test\s+(?:failed|failure|error)',
            r'(?i)tests?\s+(?:failed|failure)',
            r'(?i)assertion\s+(?:failed|error)',
            r'(?i)validation\s+(?:failed|error)',
            r'(?i)\d+\s+(?:test|tests)\s+failed',
            r'(?i)expected\s+.+\s+but\s+(?:got|was)',
            r'(?i)test\s+(?:suite|case)\s+failed',
            r'(?i)unit\s+test\s+(?:failed|error)',
            r'(?i)integration\s+test\s+(?:failed|error)',
        ]
        
        # Stack trace and traceback patterns
        self.stacktrace_patterns = [
            r'(?i)(?:stack\s+trace|traceback|backtrace)',
            r'(?i)at\s+.+\(.+:\d+\)',
            r'(?i)^\s*at\s+',
            r'(?i)^\s*#\d+\s+',
            r'(?i)caused\s+by\s*[:]\s*',
        ]
        
        # CI/CD and deployment errors
        self.cicd_patterns = [
            r'(?i)docker\s+build\s+failed',
            r'(?i)image\s+build\s+(?:failed|error)',
            r'(?i)container\s+(?:failed|error)',
            r'(?i)artifact\s+(?:creation\s+)?(?:failed|error)',
            r'(?i)packaging\s+(?:failed|error)',
            r'(?i)deployment\s+(?:failed|error|timeout)',
            r'(?i)rollback\s+(?:failed|required)',
            r'(?i)service\s+(?:unavailable|failed|down)',
            r'(?i)health\s+check\s+failed',
        ]
        
        # Build process and pipeline errors
        self.pipeline_patterns = [
            r'(?i)pipeline\s+(?:failed|error|aborted)',
            r'(?i)stage\s+(?:failed|error)',
            r'(?i)job\s+(?:failed|aborted)',
            r'(?i)build\s+(?:aborted|interrupted|cancelled)',
            r'(?i)workspace\s+(?:cleanup\s+)?(?:failed|error)',
            r'(?i)checkout\s+(?:failed|error)',
            r'(?i)script\s+(?:failed|returned\s+exit\s+code)',
        ]
        
        # Warning patterns (lower priority but contextually important)
        self.warning_patterns = [
            r'(?i)\bwarning\b(?:\s*[:]\s*|\s+)',
            r'(?i)\bwarn\b(?:\s*[:]\s*|\s+)',
            r'(?i)\bdeprecated\b',
            r'(?i)\bobsolete\b',
        ]
        
        # Critical indicators (highest priority)
        self.critical_patterns = [
            r'(?i)fatal\s+error',
            r'(?i)critical\s+(?:error|failure)',
            r'(?i)segmentation\s+fault',
            r'(?i)access\s+violation',
            r'(?i)out\s+of\s+memory',
            r'(?i)stack\s+overflow',
            r'(?i)panic',
            r'(?i)crash',
            r'(?i)abort',
            r'(?i)build\s+failed',
            r'(?i)compilation\s+failed',
            r'(?i)deployment\s+failed',
            r'(?i)pipeline\s+failed',
        ]
        
        # Combine all patterns for comprehensive search
        self.all_error_patterns = (
            self.critical_patterns +
            self.core_error_patterns +
            self.extended_error_patterns +
            self.exit_code_patterns +
            self.filesystem_patterns +
            self.network_patterns +
            self.compilation_patterns +
            self.memory_patterns +
            self.test_patterns +
            self.stacktrace_patterns +
            self.cicd_patterns +
            self.pipeline_patterns
        )
        
        # Compile patterns for better performance
        self.compiled_error_patterns = [re.compile(pattern) for pattern in self.all_error_patterns]
        self.compiled_warning_patterns = [re.compile(pattern) for pattern in self.warning_patterns]
        self.compiled_critical_patterns = [re.compile(pattern) for pattern in self.critical_patterns]
        
        # INFO/WARNING filtering patterns
        self.info_warning_patterns = [
            r'(?i)^\s*\[?\s*INFO\s*\]?',
            r'(?i)^\s*\[?\s*INFORMATION\s*\]?',
            r'(?i)^\s*\[?\s*WARNING\s*\]?',
            r'(?i)^\s*\[?\s*WARN\s*\]?',
            r'(?i)^\s*\d{4}-\d{2}-\d{2}.*INFO',
            r'(?i)^\s*\d{4}-\d{2}-\d{2}.*WARN',
        ]
        self.compiled_info_warning_patterns = [re.compile(pattern) for pattern in self.info_warning_patterns]

    def _apply_contextual_info_warning_filter(self, lines: list, relevant_indices: list, error_matches: list) -> list:
        """
        Remove INFO/WARNING lines that are not contextually relevant to errors.
        
        Args:
            lines: All log lines
            relevant_indices: Current set of relevant line indices
            error_matches: List of (line_idx, priority, line_content) tuples for errors
            
        Returns:
            Filtered list of line indices with non-contextual INFO/WARNING removed
        """
        if not error_matches:
            # If no errors found, keep everything (fallback strategy)
            return relevant_indices
        
        # Create set of error line indices for quick lookup
        error_line_indices = {match[0] for match in error_matches}
        
        # Define context window around errors (wider than extraction window)
        context_window = 12  # Lines before/after errors to preserve INFO/WARNING
        
        # Create set of "protected" indices (near errors)
        protected_indices = set()
        for error_idx in error_line_indices:
            for i in range(max(0, error_idx - context_window), 
                          min(len(lines), error_idx + context_window + 1)):
                protected_indices.add(i)
        
        # Filter out INFO/WARNING lines that are not protected
        filtered_indices = []
        info_warning_removed = 0
        
        for idx in relevant_indices:
            line = lines[idx]
            
            # Check if this line is INFO/WARNING
            is_info_warning = any(pattern.search(line) for pattern in self.compiled_info_warning_patterns)
            
            if is_info_warning and idx not in protected_indices:
                # This is an INFO/WARNING line not near any errors - remove it
                info_warning_removed += 1
                continue
            else:
                # Keep this line (either not INFO/WARNING, or it's contextually relevant)
                filtered_indices.append(idx)
        
        if info_warning_removed > 0:
            print(f"Removed {info_warning_removed} non-contextual INFO/WARNING lines")
        
        return filtered_indices

    def extract_relevant_logs(self, log_content: str, max_lines: int = 500, context_before: int = 15, context_after: int = 8) -> str:
        """
        Smart log extraction that finds error-related content with context.
        
        Args:
            log_content: Full log content as string
            max_lines: Maximum number of lines to return (default 500)
            context_before: Lines to include before each error (default 15)
            context_after: Lines to include after each error (default 8)
        
        Returns:
            Filtered log content containing error-related sections with context
        """
        if not log_content or not log_content.strip():
            return log_content
        
        lines = log_content.split('\n')
        total_lines = len(lines)
        
        # If already under limit, return as-is
        if total_lines <= max_lines:
            print(f"Log already under limit: {total_lines} <= {max_lines} lines")
            return log_content
        
        print(f"Log size: {total_lines} lines, extracting relevant content (max {max_lines} lines)")
        
        # Track relevant line indices and their priorities
        relevant_indices = set()
        error_matches = []
        
        # Find all error matches with priorities
        for i, line in enumerate(lines):
            # Check critical patterns (highest priority)
            for pattern in self.compiled_critical_patterns:
                if pattern.search(line):
                    error_matches.append((i, 'critical', line))
                    break
            else:
                # Check regular error patterns (medium priority)
                for pattern in self.compiled_error_patterns:
                    if pattern.search(line):
                        error_matches.append((i, 'error', line))
                        break
                else:
                    # Check warning patterns (low priority)
                    for pattern in self.compiled_warning_patterns:
                        if pattern.search(line):
                            error_matches.append((i, 'warning', line))
                            break
        
        print(f"Found {len(error_matches)} error/warning matches")
        
        if not error_matches:
            # No errors found - take the last portion (where failures usually occur)
            print("No error patterns found, taking last portion of log")
            start_idx = max(0, total_lines - max_lines)
            return '\n'.join(lines[start_idx:])
        
        # Sort matches by priority (critical first, then by line number)
        error_matches.sort(key=lambda x: (0 if x[1] == 'critical' else 1 if x[1] == 'error' else 2, x[0]))
        
        # Collect context around each error match
        for line_idx, priority, line_content in error_matches:
            # Add context window around the error
            start = max(0, line_idx - context_before)
            end = min(total_lines, line_idx + context_after + 1)
            
            # Add all lines in the context window
            for idx in range(start, end):
                relevant_indices.add(idx)
            
            # Stop if we have enough content
            if len(relevant_indices) >= max_lines:
                break
        
        # Convert to sorted list
        relevant_indices = sorted(list(relevant_indices))
        
        # If still too many lines, prioritize critical errors
        if len(relevant_indices) > max_lines:
            print(f"Still too many lines ({len(relevant_indices)}), prioritizing critical errors")
            
            # Find critical error sections first
            critical_indices = set()
            for line_idx, priority, _ in error_matches:
                if priority == 'critical':
                    start = max(0, line_idx - context_before)
                    end = min(total_lines, line_idx + context_after + 1)
                    for idx in range(start, end):
                        critical_indices.add(idx)
                        if len(critical_indices) >= max_lines // 2:  # Reserve half for critical
                            break
                if len(critical_indices) >= max_lines // 2:
                    break
            
            # Fill remaining space with other errors
            remaining_space = max_lines - len(critical_indices)
            other_indices = set()
            for line_idx, priority, _ in error_matches:
                if priority != 'critical' and remaining_space > 0:
                    start = max(0, line_idx - context_before // 2)  # Less context for non-critical
                    end = min(total_lines, line_idx + context_after // 2 + 1)
                    for idx in range(start, end):
                        if idx not in critical_indices:
                            other_indices.add(idx)
                            remaining_space -= 1
                            if remaining_space <= 0:
                                break
                if remaining_space <= 0:
                    break
            
            relevant_indices = sorted(list(critical_indices | other_indices))
        
        # Extract relevant lines
        if len(relevant_indices) > max_lines:
            relevant_indices = relevant_indices[:max_lines]
        
        # Apply contextual INFO/WARNING filtering
        relevant_indices = self._apply_contextual_info_warning_filter(lines, relevant_indices, error_matches)
        
        # Build the filtered log with section markers
        filtered_lines = []
        last_idx = -2
        
        for idx in relevant_indices:
            # Add section break if there's a gap
            if idx != last_idx + 1 and last_idx != -2:
                filtered_lines.append(f"\n--- Log section gap: lines {last_idx + 1}-{idx - 1} omitted ---\n")
            
            filtered_lines.append(f"{idx + 1:6d}: {lines[idx]}")
            last_idx = idx
        
        filtered_content = '\n'.join(filtered_lines)
        
        print(f"Filtered log: {len(relevant_indices)} lines from original {total_lines} lines")
        print(f"Filtered content size: {len(filtered_content)} characters")
        
        return filtered_content

    def _remove_timestamps(self, log_content: str) -> str:
        """Remove Jenkins timestamps to reduce token usage."""
        import re
        
        # Remove Jenkins timestamps like [2024-02-02T21:30:53.138Z]
        timestamp_pattern = r'\[20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\]\s*'
        cleaned_content = re.sub(timestamp_pattern, '', log_content)
        
        print(f"Timestamps removed: {len(log_content)} -> {len(cleaned_content)} characters")
        return cleaned_content

    def _azure_openai_analysis_from_url(self, failure_event: FailureEvent, jenkins_url: str) -> FailureAnalysis:
        """Enhanced LLM analysis using Azure OpenAI with URL-based context."""
        if not self.openai_client:
            logger.warning("Azure OpenAI client not available - returning mock analysis")
            print("Azure OpenAI client not available for analysis")
            return FailureAnalysis(
                category="Unknown",
                rootCause="LLM analysis not available (no API key)",
                confidence=0.0
            )
        
        try:
            print("Starting enhanced Azure OpenAI analysis from URL...")
            # Get complete job configuration and build parameters using URL
            print("Fetching job configuration and parameters from URL...")
            try:
                parameters = self.jenkins_api.fetch_build_parameters_from_url(jenkins_url)
                job_config = self.jenkins_api.fetch_job_configuration_from_url(jenkins_url)
            except Exception as e:
                logger.warning("Failed to fetch additional context", error=str(e))
                # Create minimal fallback data
                parameters = BuildParameters(parameters={})
                job_config = JobConfiguration(
                    repository=None,
                    parameterDefinitions=[],
                    triggers=None,
                    environment=None,
                    buildSteps=[],
                    scriptText="Unable to fetch job configuration",
                    description=None,
                    concurrent=False,
                    throttle=False
                )
            
            # Extract rich error context from the failure event
            error_context = failure_event.context.get('error_context', failure_event.message)
            search_strategy = failure_event.context.get('search_strategy', 'unknown')
            total_lines = failure_event.context.get('total_log_lines', 0)
            
            # Remove timestamps to reduce token usage
            error_context = self._remove_timestamps(error_context)
            
            # Smart log filtering: Extract relevant error content if logs are too large
            if total_lines > 300:  # Filter only when logs exceed 300 lines
                print(f"Large log detected ({total_lines} lines), applying smart filtering...")
                
                # Split logs into lines for processing
                lines = error_context.split('\n')
                
                # Always preserve last 100 lines (most recent/critical info)
                last_100_lines = lines[-100:]
                remaining_lines = lines[:-100]
                
                # Filter remaining lines to ~200 lines using smart extraction
                print(f"Filtering {len(remaining_lines)} lines to ~200 lines, preserving last 100 lines")
                remaining_content = '\n'.join(remaining_lines)
                filtered_remaining = self.extract_relevant_logs(remaining_content, max_lines=200, context_before=8, context_after=4)
                filtered_remaining_lines = filtered_remaining.split('\n')
                
                # Combine filtered content + preserved last 100 lines
                final_lines = filtered_remaining_lines + ["\n--- Last 100 lines preserved ---\n"] + last_100_lines
                error_context = '\n'.join(final_lines)
                
                filtered_lines = len(final_lines)
                print(f"Log filtered: {total_lines} -> {filtered_lines} lines (target: ~300, last 100 preserved)")
                # Update total lines to reflect filtered content
                total_lines = filtered_lines
            else:
                print(f"Log size acceptable ({total_lines} lines), no filtering needed")
            
            # Enhanced prompt for ANALYSIS ONLY (no code fixes)
            prompt = f"""
You are a Jenkins CI/CD expert performing DETAILED BUILD FAILURE ANALYSIS. Your role is to diagnose and categorize failures, NOT to provide code fixes.

===== FAILURE CONTEXT ANALYSIS =====
Jenkins URL: {jenkins_url}
Total Log Size: {total_lines:,} lines
Error Detection Method: {search_strategy}
Error Classification: {failure_event.type}
Failure Line: {failure_event.line}

===== OPTIMIZED ERROR CONTEXT =====
{error_context}

===== COMPLETE JENKINS CONFIGURATION =====
Repository Configuration:
- URL: {job_config.repository.url if job_config.repository else 'No SCM configured'}
- Branches: {', '.join(job_config.repository.branches) if job_config.repository and job_config.repository.branches else 'Default branch'}
- Credentials: {job_config.repository.credentialsId if job_config.repository else 'None'}

Build Parameters Analysis:
- Parameters Defined in Job: {len(job_config.parameterDefinitions)}
- Parameters Used in Build: {len(parameters.parameters)}
- Parameter Details: {[f"{p.name}={p.defaultValue}" for p in job_config.parameterDefinitions[:3]]}{'...' if len(job_config.parameterDefinitions) > 3 else ''}
- Runtime Values: {dict(list(parameters.parameters.items())[:3])}{'...' if len(parameters.parameters) > 3 else ''}

Build Environment:
- Workspace Cleanup: {job_config.environment.deleteWorkspace if job_config.environment else 'Unknown'}
- Secrets Used: {job_config.environment.useSecrets if job_config.environment else 'Unknown'}
- Timeout: {job_config.environment.timeoutMinutes if job_config.environment and job_config.environment.timeoutMinutes else 'No timeout'} minutes

Build Script Analysis:
- Script Type: {'Pipeline' if 'pipeline' in job_config.scriptText.lower()[:100] else 'Freestyle' if job_config.buildSteps else 'Unknown'}
- Script Length: {len(job_config.scriptText)} characters
- Key Script Content: {job_config.scriptText[:400] if job_config.scriptText else 'No script content'}...

===== ANALYSIS OBJECTIVES =====
Perform comprehensive failure analysis focusing on:

1. ROOT CAUSE IDENTIFICATION:
   - Identify the exact technical cause of failure
   - Determine failure category: Configuration, Code, Infrastructure, Dependencies, Environment
   - Assess impact severity and urgency

2. FAILURE CLASSIFICATION:
   - Build Process Failure (compilation, testing, packaging)
   - Environment Issues (missing dependencies, permissions, resources)
   - Configuration Problems (Jenkins setup, SCM, parameters)
   - Infrastructure Issues (server capacity, network, services)
   - Code Quality Issues (syntax, logic, test failures)

3. IMPACT ASSESSMENT:
   - Determine if this is a blocking issue
   - Assess if it affects other builds/projects
   - Evaluate reproducibility and frequency

4. DIAGNOSTIC INSIGHTS:
   - Provide detailed technical explanation
   - Identify contributing factors
   - Suggest investigation areas for developers

===== REQUIRED JSON RESPONSE =====
Provide ANALYSIS ONLY in this exact JSON structure:

{{
  "rootCause": "Detailed technical explanation of what went wrong and why, including specific error details",
  "category": "Configuration|Code|Infrastructure|Dependencies|Environment|Testing",
  "severity": "Critical|High|Medium|Low",
  "impactScope": "Single Build|Project|Multiple Projects|Infrastructure",
  "isRecurring": true|false,
  "confidence": 0.0-1.0,
  "technicalDetails": "Detailed technical context including error codes, file paths, and system information",
  "investigationAreas": "Specific areas developers should investigate further",
  "blockingFactors": "What is preventing the build from succeeding",
  "relatedSystems": "Other systems or components that might be affected"
}}

Focus on THOROUGH ANALYSIS and DIAGNOSTIC INSIGHTS. Do NOT provide code fixes or solutions - only detailed failure analysis."""
            
            print("Sending request to Azure OpenAI API...")
            print(f"Prompt length: {len(prompt)} characters")
            
            print(f"Deployment: {config.AZURE_OPENAI_DEPLOYMENT}")
            
            try:
                response = self.openai_client.chat.completions.create(
                    model=config.AZURE_OPENAI_DEPLOYMENT,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,  # Very low temperature for precise analysis
                    max_tokens=3500  # Optimized for detailed analysis responses
                )
            except Exception as api_error:
                # Handle specific API errors, especially token limit exceeded
                error_message = str(api_error).lower()
                
                if "400" in error_message or "token" in error_message or "too large" in error_message:
                    logger.error("LLM API failed due to massive logs", 
                               prompt_length=len(prompt), 
                               total_lines=total_lines,
                               error=str(api_error))
                    
                    return FailureAnalysis(
                        category="Analysis Failed",
                        rootCause=f"❌ Analysis failed: Logs too massive for AI processing (Prompt: {len(prompt):,} characters, {total_lines:,} lines). Even after smart filtering, the logs exceed token limits. Please try with a smaller build or contact support for manual analysis.",
                        confidence=0.0,
                        severity="High",
                        impactScope="Analysis Tool",
                        isRecurring=False,
                        technicalDetails=f"Token limit exceeded error from Azure OpenAI API. Original log size: {total_lines:,} lines. Filtered prompt size: {len(prompt):,} characters. Error: {str(api_error)[:200]}",
                        investigationAreas="Try analyzing individual stages manually, reduce log verbosity, or use smaller build for analysis",
                        blockingFactors="Build logs exceed maximum token processing capacity even after smart filtering",
                        relatedSystems="Azure OpenAI API token limits, Jenkins log size management"
                    )
                else:
                    # Other API errors (network, auth, etc.)
                    logger.error("LLM API failed with unexpected error", error=str(api_error))
                    
                    return FailureAnalysis(
                        category="Analysis Failed",
                        rootCause=f"❌ Analysis failed: API error occurred during processing. Error: {str(api_error)[:100]}",
                        confidence=0.0,
                        severity="Medium",
                        impactScope="Analysis Tool",
                        isRecurring=False,
                        technicalDetails=f"Azure OpenAI API error: {str(api_error)}",
                        investigationAreas="Check API connectivity and authentication, retry analysis",
                        blockingFactors="API communication failure",
                        relatedSystems="Azure OpenAI API, network connectivity"
                    )
            
            print("Azure OpenAI API response received")
            content = response.choices[0].message.content
            print(f"Response length: {len(content)} characters")
            logger.info("Azure OpenAI analysis response", content=content[:200])
            
            # Try to parse JSON response
            try:
                import json
                import re
                
                # Clean up markdown-wrapped JSON response
                cleaned_content = content.strip()
                if cleaned_content.startswith('```json'):
                    # Extract JSON from markdown code block
                    json_match = re.search(r'```json\s*\n(.*?)\n```', cleaned_content, re.DOTALL)
                    if json_match:
                        cleaned_content = json_match.group(1).strip()
                elif cleaned_content.startswith('```'):
                    # Handle generic code block
                    json_match = re.search(r'```\s*\n(.*?)\n```', cleaned_content, re.DOTALL)
                    if json_match:
                        cleaned_content = json_match.group(1).strip()
                
                parsed = json.loads(cleaned_content)
                
                return FailureAnalysis(
                    category=parsed.get("category", "Unknown"),
                    rootCause=parsed.get("rootCause", content[:1000] if content else "No analysis available"),
                    confidence=float(parsed.get("confidence", 0.7)),
                    severity=parsed.get("severity", "Medium"),
                    impactScope=parsed.get("impactScope", "Single Build"),
                    isRecurring=parsed.get("isRecurring", False),
                    technicalDetails=parsed.get("technicalDetails", ""),
                    investigationAreas=parsed.get("investigationAreas", ""),
                    blockingFactors=parsed.get("blockingFactors", ""),
                    relatedSystems=parsed.get("relatedSystems", "")
                )
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to parse JSON response, using fallback", error=str(e))
                
                # Enhanced fallback parsing with keyword detection
                return FailureAnalysis(
                    category="Code" if any(keyword in content.lower() for keyword in ['compilation', 'syntax', 'test', 'build']) else "Infrastructure",
                    rootCause=content[:1000] if content else "Analysis completed",
                    confidence=0.6,
                    severity="Medium",
                    technicalDetails=content if content else "No specific details available"
                )
                
        except Exception as e:
            logger.error("Enhanced LLM analysis failed", error=str(e), jenkins_url=jenkins_url)
            import traceback
            traceback.print_exc()  # Print full traceback for debugging
            
            return FailureAnalysis(
                category="Unknown",
                rootCause=f"Analysis failed: {str(e)}",
                confidence=0.0,
                severity="High",
                technicalDetails=f"Analysis error: {str(e)}"
            )
        """Enhanced LLM analysis using Azure OpenAI GPT-5 with precise error context from optimized parsing."""
        if not self.openai_client:
            logger.warning("Azure OpenAI client not available - returning mock analysis")
            print("Azure OpenAI client not available for analysis")
            return FailureAnalysis(
                category="Unknown",
                rootCause="LLM analysis not available (no API key)",
                confidence=0.0
            )
        
        try:
            print("Starting enhanced GROQ analysis...")
            # Get complete job configuration and build parameters
            print("Fetching job configuration and parameters...")
            parameters = self.jenkins_api.fetch_build_parameters(job_name, build_number)
            job_config = self.jenkins_api.fetch_job_configuration(job_name)
            
            # Extract rich error context from the failure event
            error_context = failure_event.context.get('error_context', failure_event.message)
            search_strategy = failure_event.context.get('search_strategy', 'unknown')
            total_lines = failure_event.context.get('total_log_lines', 0)
            
            # Enhanced prompt for ANALYSIS ONLY (no code fixes)
            prompt = f"""
You are a Jenkins CI/CD expert performing DETAILED BUILD FAILURE ANALYSIS. Your role is to diagnose and categorize failures, NOT to provide code fixes.

===== FAILURE CONTEXT ANALYSIS =====
Job: {job_name} | Build: #{build_number}
Total Log Size: {total_lines:,} lines
Error Detection Method: {search_strategy}
Error Classification: {failure_event.type}
Failure Line: {failure_event.line}

===== OPTIMIZED ERROR CONTEXT =====
{error_context}

===== COMPLETE JENKINS CONFIGURATION =====
Repository Configuration:
- URL: {job_config.repository.url if job_config.repository else 'No SCM configured'}
- Branches: {', '.join(job_config.repository.branches) if job_config.repository and job_config.repository.branches else 'Default branch'}
- Credentials: {job_config.repository.credentialsId if job_config.repository else 'None'}

Build Parameters Analysis:
- Parameters Defined in Job: {len(job_config.parameterDefinitions)}
- Parameters Used in Build: {len(parameters.parameters)}
- Parameter Details: {[f"{p.name}={p.defaultValue}" for p in job_config.parameterDefinitions[:3]]}{'...' if len(job_config.parameterDefinitions) > 3 else ''}
- Runtime Values: {dict(list(parameters.parameters.items())[:3])}{'...' if len(parameters.parameters) > 3 else ''}

Build Environment:
- Workspace Cleanup: {job_config.environment.deleteWorkspace if job_config.environment else 'Unknown'}
- Secrets Used: {job_config.environment.useSecrets if job_config.environment else 'Unknown'}
- Timeout: {job_config.environment.timeoutMinutes if job_config.environment and job_config.environment.timeoutMinutes else 'No timeout'} minutes

Build Script Analysis:
- Script Type: {'Pipeline' if 'pipeline' in job_config.scriptText.lower()[:100] else 'Freestyle' if job_config.buildSteps else 'Unknown'}
- Script Length: {len(job_config.scriptText)} characters
- Key Script Content: {job_config.scriptText[:400] if job_config.scriptText else 'No script content'}...

===== ANALYSIS OBJECTIVES =====
Perform comprehensive failure analysis focusing on:

1. ROOT CAUSE IDENTIFICATION:
   - Identify the exact technical cause of failure
   - Determine failure category: Configuration, Code, Infrastructure, Dependencies, Environment
   - Assess impact severity and urgency

2. FAILURE CLASSIFICATION:
   - Build Process Failure (compilation, testing, packaging)
   - Environment Issues (missing dependencies, permissions, resources)
   - Configuration Problems (Jenkins setup, SCM, parameters)
   - Infrastructure Issues (server capacity, network, services)
   - Code Quality Issues (syntax, logic, test failures)

3. IMPACT ASSESSMENT:
   - Determine if this is a blocking issue
   - Assess if it affects other builds/projects
   - Evaluate reproducibility and frequency

4. DIAGNOSTIC INSIGHTS:
   - Provide detailed technical explanation
   - Identify contributing factors
   - Suggest investigation areas for developers

===== REQUIRED JSON RESPONSE =====
Provide ANALYSIS ONLY in this exact JSON structure:

{{
  "rootCause": "Detailed technical explanation of what went wrong and why, including specific error details",
  "category": "Configuration|Code|Infrastructure|Dependencies|Environment|Testing",
  "severity": "Critical|High|Medium|Low",
  "impactScope": "Single Build|Project|Multiple Projects|Infrastructure",
  "isRecurring": true|false,
  "confidence": 0.0-1.0,
  "technicalDetails": "Detailed technical context including error codes, file paths, and system information",
  "investigationAreas": "Specific areas developers should investigate further",
  "blockingFactors": "What is preventing the build from succeeding",
  "relatedSystems": "Other systems or components that might be affected"
}}

Focus on THOROUGH ANALYSIS and DIAGNOSTIC INSIGHTS. Do NOT provide code fixes or solutions - only detailed failure analysis."""
            
            print("Sending request to Azure OpenAI API...")
            print(f"Prompt length: {len(prompt)} characters")
            print(f"Deployment: {config.AZURE_OPENAI_DEPLOYMENT}")
            
            response = self.openai_client.chat.completions.create(
                model=config.AZURE_OPENAI_DEPLOYMENT,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,  # Very low temperature for precise analysis
                max_tokens=3500  # Optimized for detailed analysis responses
            )
            
            print("Azure OpenAI API response received")
            content = response.choices[0].message.content
            print(f"Response length: {len(content)} characters")
            logger.info("Azure OpenAI analysis response", content=content[:200])
            
            # Try to parse JSON response
            try:
                import json
                parsed = json.loads(content)
                
                return FailureAnalysis(
                    category=parsed.get("category", "Unknown"),
                    rootCause=parsed.get("rootCause", content[:1000] if content else "No analysis available"),
                    confidence=float(parsed.get("confidence", 0.7)),
                    severity=parsed.get("severity", "Medium"),
                    impactScope=parsed.get("impactScope", "Single Build"),
                    isRecurring=parsed.get("isRecurring", False),
                    technicalDetails=parsed.get("technicalDetails", ""),
                    investigationAreas=parsed.get("investigationAreas", ""),
                    blockingFactors=parsed.get("blockingFactors", ""),
                    relatedSystems=parsed.get("relatedSystems", "")
                )
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to parse JSON response, using fallback", error=str(e))
                
                # Enhanced fallback parsing with keyword detection
                return FailureAnalysis(
                    category="Code" if any(keyword in content.lower() for keyword in ['compilation', 'syntax', 'test', 'build']) else "Infrastructure",
                    rootCause=content[:1000] if content else "Analysis completed",
                    confidence=0.6,
                    severity="Medium",
                    technicalDetails=content if content else "No specific details available"
                )
                
        except Exception as e:
            logger.error("Enhanced LLM analysis failed", error=str(e), job_name=job_name)
            import traceback
            traceback.print_exc()  # Print full traceback for debugging
            
            return FailureAnalysis(
                category="Unknown",
                rootCause=f"Analysis failed: {str(e)}",
                confidence=0.0,
                severity="High",
                technicalDetails=f"Analysis error: {str(e)}"
            )

    def analyze_failure_from_url(self, failure: FailureEvent, jenkins_url: str) -> FailureAnalysis:
        """Analyze failure using optimized context from parse_failure_events and Azure OpenAI with URL-based approach."""
        try:
            # The failure event already contains rich context from parse_failure_events
            # No need to re-extract error context - use the optimized context directly
            logger.info("Using Azure OpenAI analysis with optimized failure context from URL", 
                       error_type=failure.type, 
                       line=failure.line,
                       strategy=failure.context.get('search_strategy', 'unknown'),
                       jenkins_url=jenkins_url)
            
            return self._azure_openai_analysis_from_url(failure, jenkins_url)
            
        except Exception as e:
            logger.error("Failed to analyze failure from URL", error=str(e))
            raise


class CodeFixSuggester:
    def __init__(self):
        # Initialize Azure OpenAI client for code fix suggestions
        self.openai_client = None
        if AZURE_OPENAI_AVAILABLE and config.AZURE_OPENAI_API_KEY and config.AZURE_OPENAI_API_KEY != "":
            try:
                # Workaround for container environment proxy parameter issue
                try:
                    self.openai_client = AzureOpenAI(
                        api_key=config.AZURE_OPENAI_API_KEY,
                        api_version=config.AZURE_OPENAI_API_VERSION,
                        azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
                        timeout=30.0
                    )
                except TypeError as e:
                    if "proxies" in str(e):
                        # Fallback without timeout parameter
                        self.openai_client = AzureOpenAI(
                            api_key=config.AZURE_OPENAI_API_KEY,
                            api_version=config.AZURE_OPENAI_API_VERSION,
                            azure_endpoint=config.AZURE_OPENAI_ENDPOINT
                        )
                    else:
                        raise e
                        
                logger.info("CodeFixSuggester Azure OpenAI client initialized", deployment=config.AZURE_OPENAI_DEPLOYMENT)
            except Exception as e:
                logger.warning("Failed to initialize CodeFixSuggester Azure OpenAI client", error=str(e))
                self.openai_client = None
    
    def suggest_code_fix(self, failure_analysis: FailureAnalysis, job_name: str, build_number: int) -> CodeFixSuggestion:
        """Generate code fix suggestions based on failure analysis."""
        try:
            if not self.openai_client:
                return self._generate_manual_fix_suggestion(failure_analysis)
            
            # Enhanced prompt for CODE FIX GENERATION
            prompt = f"""
You are a Jenkins CI/CD automation expert specializing in AUTOMATED CODE FIXES and SOLUTION IMPLEMENTATION.

===== FAILURE ANALYSIS INPUT =====
Job: {job_name} | Build: #{build_number}
Category: {failure_analysis.category}
Root Cause: {failure_analysis.rootCause}
Severity: {failure_analysis.severity}
Impact Scope: {failure_analysis.impactScope}
Technical Details: {failure_analysis.technicalDetails}
Blocking Factors: {failure_analysis.blockingFactors}
Confidence: {failure_analysis.confidence:.2f}

===== CODE FIX GENERATION OBJECTIVES =====
Based on the failure analysis, provide:

1. AUTOMATED FIX ASSESSMENT:
   - Determine if this can be automatically fixed
   - Identify the fix type: Configuration, Code, Infrastructure, or Manual intervention
   - Assess automation feasibility

2. SPECIFIC FIX INSTRUCTIONS:
   - Provide exact step-by-step instructions
   - Include specific file paths, configuration changes, and code modifications
   - Generate ready-to-use code snippets where applicable

===== REQUIRED JSON RESPONSE =====
{{
  "fixType": "Configuration|Code|Infrastructure|Manual",
  "isAutomatable": true|false,
  "suggestedFix": "Concise fix description",
  "fixLocation": "Specific location where fix should be applied",
  "stepByStepInstructions": [
    "Step 1: Specific action to take",
    "Step 2: Next action with exact commands/changes",
    "Step 3: Verification steps"
  ],
  "filesToModify": [
    "path/to/file1.ext",
    "path/to/file2.ext"
  ],
  "configurationChanges": {{
    "jenkins_parameter": "new_value",
    "environment_variable": "required_setting"
  }},
  "manualActions": [
    "Actions that require human intervention",
    "System admin tasks"
  ]
}}

Focus on ACTIONABLE, SPECIFIC solutions that developers can immediately implement."""
            
            try:
                response = self.openai_client.chat.completions.create(
                    model=config.AZURE_OPENAI_DEPLOYMENT,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=3500  # Consistent limit for detailed responses
                )
                
                content = response.choices[0].message.content
            except Exception as api_error:
                # Handle API errors in CodeFixSuggester
                error_message = str(api_error).lower()
                
                if "400" in error_message or "token" in error_message or "too large" in error_message:
                    logger.error("CodeFixSuggester API failed due to massive input", 
                               prompt_length=len(prompt),
                               error=str(api_error))
                    
                    return CodeFixSuggestion(
                        analysisId=f"{job_name}_{build_number}_token_limit_exceeded",
                        fixType="Manual",
                        isAutomatable=False,
                        suggestedFix="❌ Fix generation failed: Input too large for AI processing. The failure analysis contains too much data for automated fix generation.",
                        fixLocation="Manual analysis required",
                        stepByStepInstructions=[
                            "Review the failure analysis manually",
                            "Break down the problem into smaller components",
                            "Apply fixes based on the error category and technical details",
                            "Test changes incrementally"
                        ],
                        configurationChanges={},
                        manualActions=[
                            "Analysis exceeds AI processing capacity - manual intervention required",
                            "Consider reducing log verbosity for future builds",
                            "Contact support team for complex failure analysis"
                        ]
                    )
                else:
                    # Other API errors
                    logger.error("CodeFixSuggester API failed with unexpected error", error=str(api_error))
                    
                    return CodeFixSuggestion(
                        analysisId=f"{job_name}_{build_number}_api_error",
                        fixType="Manual", 
                        isAutomatable=False,
                        suggestedFix=f"❌ Fix generation failed: API error occurred. {str(api_error)[:100]}",
                        fixLocation="API communication issue",
                        stepByStepInstructions=[
                            "Retry the fix generation after a few minutes", 
                            "Check network connectivity to Azure OpenAI",
                            "Review the failure analysis manually for fix guidance"
                        ],
                        manualActions=[
                            "API communication failure - manual analysis required",
                            "Retry automated analysis after resolving connectivity"
                        ]
                    )
            print(f"CodeFixSuggester response length: {len(content)} characters")
            print(f"CodeFixSuggester response preview: {content[:300]}...")
            logger.info("CodeFixSuggester response received", content=content[:200])
            
            # Parse the JSON response
            try:
                import json
                import re
                
                # Clean up markdown-wrapped JSON response (same fix as analysis method)
                cleaned_content = content.strip()
                print(f"Original content starts with: {cleaned_content[:50]}")
                
                if cleaned_content.startswith('```json'):
                    # Extract JSON from markdown code block
                    json_match = re.search(r'```json\s*\n(.*?)\n```', cleaned_content, re.DOTALL)
                    if json_match:
                        cleaned_content = json_match.group(1).strip()
                        print("Extracted JSON from ```json``` block")
                elif cleaned_content.startswith('```'):
                    # Handle generic code block
                    json_match = re.search(r'```\s*\n(.*?)\n```', cleaned_content, re.DOTALL)
                    if json_match:
                        cleaned_content = json_match.group(1).strip()
                        print("Extracted JSON from generic ``` block")
                
                print(f"Cleaned content for parsing: {cleaned_content[:200]}...")
                parsed = json.loads(cleaned_content)
                print("JSON parsing successful!")
                
                return CodeFixSuggestion(
                    analysisId=f"{job_name}_{build_number}_{failure_analysis.category}",
                    fixType=parsed.get("fixType", "Manual"),
                    isAutomatable=parsed.get("isAutomatable", False),
                    suggestedFix=parsed.get("suggestedFix", "Manual investigation required"),
                    fixLocation=parsed.get("fixLocation", "Unknown"),
                    stepByStepInstructions=parsed.get("stepByStepInstructions", []),
                    filesToModify=parsed.get("filesToModify", []),
                    configurationChanges=parsed.get("configurationChanges", {}),
                    manualActions=parsed.get("manualActions", [])
                )
                
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to parse CodeFixSuggester JSON response", error=str(e))
                return self._generate_fallback_fix_suggestion(failure_analysis, job_name, build_number, content)
                
        except Exception as e:
            logger.error("CodeFixSuggester failed", error=str(e))
            return self._generate_manual_fix_suggestion(failure_analysis)
    
    def _generate_manual_fix_suggestion(self, failure_analysis: FailureAnalysis) -> CodeFixSuggestion:
        """Generate a basic manual fix suggestion when AI is not available."""
        return CodeFixSuggestion(
            analysisId=f"manual_{failure_analysis.category}",
            fixType="Manual",
            isAutomatable=False,
            suggestedFix="Manual investigation and fixing required",
            fixLocation="Multiple locations may need attention",
            stepByStepInstructions=[
                "Review the failure analysis details",
                "Investigate the root cause areas identified",
                "Apply appropriate fixes based on the category",
                "Test the changes locally before committing"
            ],
            manualActions=[
                "Review build logs manually",
                "Consult with team members if needed",
                "Apply fixes based on error category"
            ]
        )
    
    def _generate_fallback_fix_suggestion(self, failure_analysis: FailureAnalysis, job_name: str, build_number: int, ai_content: str) -> CodeFixSuggestion:
        """Generate fallback suggestion when JSON parsing fails, trying to extract useful information."""
        
        # Try to extract step-by-step instructions from the raw content
        step_instructions = []
        if ai_content:
            # Look for numbered steps in the content
            import re
            step_matches = re.findall(r'(\d+\.\s+[^\n]+)', ai_content)
            if step_matches:
                step_instructions = [step.strip() for step in step_matches[:10]]  # Limit to 10 steps
            
            # If no numbered steps found, try to extract useful sentences
            if not step_instructions:
                sentences = [s.strip() for s in ai_content.split('.') if s.strip() and len(s.strip()) > 20]
                step_instructions = sentences[:5]  # Take first 5 meaningful sentences
        
        # Default steps if nothing extracted
        if not step_instructions:
            step_instructions = [
                "Review the failure analysis details above",
                "Check the technical details and blocking factors",
                "Investigate the root cause areas identified",
                "Apply appropriate fixes based on the analysis",
                "Test the changes locally before committing"
            ]
        
        return CodeFixSuggestion(
            analysisId=f"{job_name}_{build_number}_fallback",
            fixType="Manual",
            isAutomatable=False,
            suggestedFix=ai_content[:800] if ai_content else "Review failure analysis for guidance",
            fixLocation="See analysis details",
            stepByStepInstructions=step_instructions,
            manualActions=[
                "Review the AI-generated content above",
                "Extract actionable steps manually",
                "Consult team members if needed",
                "Apply fixes based on the failure category"
            ]
        )