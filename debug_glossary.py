import yaml
import sys
from src.core.llm_client import LLMManager
from src.agents.term_extractor import TermExtractor

# 强制 stdout utf-8
if sys.platform.startswith('win'):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 读取配置
with open("config/config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

llm_manager = LLMManager(config["llm"])
extractor = TermExtractor(llm_manager)

sample_text = """
It is possible I already had some presentiment of my future. The locked and rusted gate that stood before us, with wisps of river fog threading its spikes like the mountain paths, remains in my mind now as the symbol of my exile. That is why I have begun this account of it with the aftermath of our swim, in which I, the torturer’s apprentice Severian, had so nearly drowned.

“The guard has gone.” Thus my friend Roche spoke to Drotte, who had already seen it for himself.

“And try to get through the barbican without a safe-conduct? They’d send to Master Gurloes.”
"""

print("Running Term Extraction...")
terms = extractor.extract(sample_text)

print(f"\nExtracted {len(terms)} terms:")
for t in terms:
    print(f"- [{t.get('category')}] {t.get('src')}: {t.get('context')}")
