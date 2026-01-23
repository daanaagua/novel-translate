"""
逻辑分析模块
负责识别和解析文本中的指代歧义
"""
import re
from typing import List, Optional, Dict, Any

from ..core.schemas import LogicAnalysisResult, AmbiguityAnalysis, TextChunk
from ..core.llm_client import LLMManager


class LogicAnalyzer:
    """
    逻辑分析器
    用于在翻译前检测和解析复杂的指代关系
    """
    
    # 默认触发词
    DEFAULT_TRIGGER_PATTERNS = [
        r'\bI mean\b',
        r'\bWhich is\b',
        r'\bThat is\b',
        r'\bIn other words\b',
        r'\bNot that\b',
        r'\bBy which I mean\b',
    ]
    
    def __init__(
        self,
        llm_manager: LLMManager,
        trigger_patterns: Optional[List[str]] = None,
        prompts: Optional[Dict[str, Any]] = None
    ):
        """
        初始化逻辑分析器
        
        Args:
            llm_manager: LLM 管理器
            trigger_patterns: 触发分析的关键词正则列表
            prompts: Prompt 配置
        """
        self.llm = llm_manager
        self.trigger_patterns = trigger_patterns or self.DEFAULT_TRIGGER_PATTERNS
        self.prompts = prompts or {}
    
    def needs_analysis(self, text: str) -> bool:
        """
        判断文本是否需要逻辑分析
        
        Args:
            text: 待检测文本
        
        Returns:
            是否包含可能的歧义结构
        """
        for pattern in self.trigger_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False
    
    def analyze(self, text: str) -> LogicAnalysisResult:
        """
        分析文本中的逻辑歧义
        
        Args:
            text: 待分析文本
        
        Returns:
            LogicAnalysisResult 对象
        """
        if not self.needs_analysis(text):
            return LogicAnalysisResult(has_ambiguity=False)
        
        # 构建 Prompt
        system_prompt = self.prompts.get("system", self._default_system_prompt())
        user_prompt = self.prompts.get("user", "{source_text}").format(source_text=text)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        # 调用 LLM
        try:
            result = self.llm.chat_json(
                messages=messages,
                purpose="logic",
                temperature=0.1
            )
            
            # 解析结果
            return self._parse_result(result)
        
        except Exception as e:
            # 分析失败，返回空结果（不阻塞翻译流程）
            print(f"[LogicAnalyzer] 分析失败: {e}")
            return LogicAnalysisResult(has_ambiguity=False)
    
    def analyze_chunk(self, chunk: TextChunk) -> TextChunk:
        """
        分析 Chunk 并更新其 logic_analysis 字段
        
        Args:
            chunk: 待分析的 TextChunk
        
        Returns:
            更新后的 TextChunk
        """
        result = self.analyze(chunk.source_text)
        chunk.logic_analysis = result
        return chunk
    
    def _parse_result(self, raw: Dict[str, Any]) -> LogicAnalysisResult:
        """解析 LLM 返回的 JSON"""
        has_ambiguity = raw.get("has_ambiguity", False)
        
        analysis_list = []
        for item in raw.get("analysis", []):
            try:
                analysis = AmbiguityAnalysis(
                    quote=item.get("quote", ""),
                    connects_to=item.get("connects_to", ""),
                    interpretation=item.get("interpretation", ""),
                    disconnection=item.get("disconnection")
                )
                analysis_list.append(analysis)
            except Exception:
                continue
        
        return LogicAnalysisResult(
            has_ambiguity=has_ambiguity or len(analysis_list) > 0,
            analysis=analysis_list
        )
    
    def _default_system_prompt(self) -> str:
        """默认的逻辑分析 System Prompt"""
        return """你是一个精通逻辑分析的文学翻译专家。你的任务是分析复杂英文小说文本中的指代关系和逻辑衔接。

Gene Wolfe 等高阶作家的小说中，对话经常被叙述打断，后一句常是对前几句的补充说明（而非紧接上一句）。

遇到 "I mean", "That is", "Which is" 等补充说明性的句子时，你必须：
1. 找到它具体修饰前文的哪句话？
2. 排除它修饰临近句子的可能性（如果逻辑不通）。
3. 在 JSON 中输出你的分析。

请严格按以下 JSON 格式输出：
{
    "has_ambiguity": true/false,
    "analysis": [
        {
            "quote": "原文中有歧义的句子",
            "connects_to": "它修饰的前文句子",
            "interpretation": "用中文解释实际含义",
            "disconnection": "明确它不是修饰哪句话"
        }
    ]
}"""
