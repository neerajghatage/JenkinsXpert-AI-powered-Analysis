import os
import sys
import logging
import structlog
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class Config:
    # Jenkins Configuration
    JENKINS_URL = os.getenv("JENKINS_URL", "")
    JENKINS_USERNAME = os.getenv("JENKINS_USERNAME", "")
    JENKINS_API_TOKEN = os.getenv("JENKINS_API_TOKEN", "")
    
    # Azure OpenAI Configuration
    AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
    AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
    AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "")
    
    # Repository Configuration
    REPO_PATH = os.getenv("REPO_PATH", "")
    
    # Logging Configuration
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Setup structured logging
def setup_logging():
    import sys
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, Config.LOG_LEVEL.upper())
    )
    
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer()
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

config = Config()
setup_logging()
logger = structlog.get_logger(__name__)