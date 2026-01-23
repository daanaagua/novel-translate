from typing import List, Dict, Any
import json
from ..core.llm_client import LLMManager
from ..core.schemas import GlossaryItem

class TermExtractor:
    """
    术语提取 Agent
    """
    def __init__(self, llm_manager: LLMManager):
        self.llm = llm_manager
    
    def extract(self, text: str) -> List[Dict[str, Any]]:
        """
        从文本中提取潜在术语
        """
        system_prompt = """你是一个术语提取专家。请从小说文本中提取所有专有名词，包括：
- 人名 (Person)
- 地名 (Place)
- 组织/势力 (Organization)
- 特殊物品/武器 (Item)
- 特有概念/造词 (Concept)

请以 JSON 列表格式输出，每项包含：
{
    "src": "原文",
    "category": "分类",
    "context": "该词出现的简短上下文"
}

只提取真正独特的专有名词，忽略普通单词。
"""
        try:
            result = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text}
                ],
                purpose="logic", # 使用逻辑模型更准确
                temperature=0.1
            )
            
            # 兼容各种 JSON 结构
            items = []
            if isinstance(result, list):
                items = result
            elif isinstance(result, dict):
                items = result.get("terms", result.get("items", []))
            
            return items
            
        except Exception as e:
            print(f"[TermExtractor] 提取失败: {e}")
            return []
