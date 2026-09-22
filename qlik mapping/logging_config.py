import logging

class AgentLoggerAdapter(logging.LoggerAdapter):
    """Custom logging adapter to include agent name in log outputs."""
    def process(self, msg, kwargs):
        agent_name = self.extra.get('agent_name', 'Unknown')
        return f"[{agent_name}] {msg}", kwargs


def setup_logging(level: int = logging.INFO) -> AgentLoggerAdapter:
    """Set up base logging configuration and return main logger adapter."""
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S %Z'
    )
    return AgentLoggerAdapter(logging.getLogger(__name__), {'agent_name': 'Main'})

logger = setup_logging()
