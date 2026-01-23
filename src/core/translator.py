"""
翻译引擎模块
核心翻译流程：逻辑分析 -> 直译 -> 润色
"""
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from .schemas import TextChunk, ChunkStatus, LogicAnalysisResult
from .llm_client import LLMManager
from ..agents.logic_analyzer import LogicAnalyzer
from ..agents.glossary_manager import GlossaryManager


@dataclass
class TranslationConfig:
    """翻译配置"""
    # 直译参数
    draft_temperature: float = 0.1
    draft_max_tokens: int = 4096
    
    # 润色参数
    polish_temperature: float = 0.7
    polish_max_tokens: int = 4096
    
    # 是否启用逻辑分析
    enable_logic_analysis: bool = True
    
    # 是否启用润色
    enable_polish: bool = True
    
    # 风格参考文本
    style_reference: Optional[str] = None


class TranslationEngine:
    """
    翻译引擎
    实现 PRD 中的 "直译 -> 润色" 双流架构
    """
    
    def __init__(
        self,
        llm_manager: LLMManager,
        glossary_manager: GlossaryManager,
        prompts: Dict[str, Any],
        config: Optional[TranslationConfig] = None
    ):
        """
        初始化翻译引擎
        
        Args:
            llm_manager: LLM 管理器
            glossary_manager: 术语表管理器
            prompts: Prompt 配置（从 prompts.yaml 加载）
            config: 翻译配置
        """
        self.llm = llm_manager
        self.glossary = glossary_manager
        self.prompts = prompts
        self.config = config or TranslationConfig()
        
        # 初始化逻辑分析器
        self.logic_analyzer = LogicAnalyzer(
            llm_manager=llm_manager,
            prompts=prompts.get("logic_analysis", {})
        )
        
        # 上下文缓存（用于滑动窗口）
        self._context_buffer: List[str] = []
        self._last_chunk_translation: Optional[str] = None
    
    def translate_chunk(
        self,
        chunk: TextChunk,
        previous_summary: Optional[str] = None
    ) -> TextChunk:
        """
        翻译单个文本块
        
        完整流程：
        1. 逻辑分析（如需要）
        2. 直译
        3. 润色
        
        Args:
            chunk: 待翻译的 TextChunk
            previous_summary: 前文摘要（上下文）
        
        Returns:
            翻译完成的 TextChunk
        """
        try:
            # 步骤 1: 逻辑分析
            if self.config.enable_logic_analysis:
                chunk.status = ChunkStatus.ANALYZING
                chunk = self.logic_analyzer.analyze_chunk(chunk)
            
            # 步骤 2: 直译
            chunk.status = ChunkStatus.DRAFTING
            chunk.draft_translation = self._draft_translate(
                source_text=chunk.source_text,
                logic_analysis=chunk.logic_analysis,
                previous_summary=previous_summary
            )
            
            # 步骤 3: 润色
            if self.config.enable_polish:
                chunk.status = ChunkStatus.POLISHING
                chunk.polished_translation = self._polish_translate(
                    source_text=chunk.source_text,
                    draft_translation=chunk.draft_translation
                )
                chunk.final_translation = chunk.polished_translation
            else:
                chunk.final_translation = chunk.draft_translation
            
            chunk.status = ChunkStatus.COMPLETED
            
            # 更新上下文缓存
            self._last_chunk_translation = chunk.final_translation
            
        except Exception as e:
            chunk.status = ChunkStatus.FAILED
            chunk.error_message = str(e)
        
        return chunk
    
    def _draft_translate(
        self,
        source_text: str,
        logic_analysis: Optional[LogicAnalysisResult] = None,
        previous_summary: Optional[str] = None
    ) -> str:
        """
        直译阶段
        
        Args:
            source_text: 原文
            logic_analysis: 逻辑分析结果
            previous_summary: 前文摘要
        
        Returns:
            直译文本
        """
        # 获取术语规则
        glossary_rules = self.glossary.get_prompt_text(source_text)
        
        # 获取逻辑分析提示
        logic_text = "(无特殊逻辑注意事项)"
        if logic_analysis and logic_analysis.has_ambiguity:
            logic_text = logic_analysis.to_prompt_text()
        
        # 构建 System Prompt
        system_template = self.prompts.get("draft", {}).get("system", self._default_draft_system())
        system_prompt = system_template.format(
            glossary_rules=glossary_rules,
            logic_analysis=logic_text
        )
        
        # 构建 User Prompt
        user_template = self.prompts.get("draft", {}).get("user", "请翻译以下文本：\n\n{source_text}")
        user_prompt = user_template.format(source_text=source_text)
        
        # 如果有前文摘要，添加上下文
        if previous_summary:
            user_prompt = f"【前文摘要】\n{previous_summary}\n\n" + user_prompt
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        return self.llm.chat(
            messages=messages,
            purpose="draft",
            temperature=self.config.draft_temperature,
            max_tokens=self.config.draft_max_tokens
        )
    
    def _polish_translate(
        self,
        source_text: str,
        draft_translation: str
    ) -> str:
        """
        润色阶段
        
        Args:
            source_text: 原文
            draft_translation: 直译文本
        
        Returns:
            润色后的文本
        """
        # 构建 System Prompt
        system_template = self.prompts.get("polish", {}).get("system", self._default_polish_system())
        system_prompt = system_template.format(
            style_reference=self.config.style_reference or "(无特定风格参考)"
        )
        
        # 构建 User Prompt
        user_template = self.prompts.get("polish", {}).get("user", 
            "## 原文\n{source_text}\n\n## 初译稿\n{draft_translation}\n\n请润色上述初译稿，输出最终译文。")
        user_prompt = user_template.format(
            source_text=source_text,
            draft_translation=draft_translation
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        return self.llm.chat(
            messages=messages,
            purpose="polish",
            temperature=self.config.polish_temperature,
            max_tokens=self.config.polish_max_tokens
        )
    
    def translate_text(
        self,
        source_text: str,
        enable_logic_analysis: bool = True,
        enable_polish: bool = True
    ) -> Dict[str, Any]:
        """
        翻译单段文本（便捷方法）
        
        Args:
            source_text: 原文
            enable_logic_analysis: 是否启用逻辑分析
            enable_polish: 是否启用润色
        
        Returns:
            包含各阶段结果的字典
        """
        result = {
            "source": source_text,
            "logic_analysis": None,
            "draft": None,
            "polished": None,
            "final": None
        }
        
        # 逻辑分析
        logic_result = None
        if enable_logic_analysis:
            logic_result = self.logic_analyzer.analyze(source_text)
            result["logic_analysis"] = logic_result.model_dump() if logic_result.has_ambiguity else None
        
        # 直译
        draft = self._draft_translate(source_text, logic_result)
        result["draft"] = draft
        
        # 润色
        if enable_polish:
            polished = self._polish_translate(source_text, draft)
            result["polished"] = polished
            result["final"] = polished
        else:
            result["final"] = draft
        
        return result
    
    def _default_draft_system(self) -> str:
        """默认直译 System Prompt"""
        return """你是一位严谨的文学翻译家。你的任务是将英文小说翻译成中文。

## 核心原则
1. **准确第一**：绝对忠于原文的事实和逻辑，不添加不删减。
2. **术语一致**：严格遵守提供的术语表规则。
3. **逻辑遵从**：如果提供了逻辑分析，必须按照分析结果处理指代关系。

## 术语表
{glossary_rules}

## 逻辑分析
{logic_analysis}

## 输出要求
- 直接输出中文译文，保持原文的分段格式。
- 不要添加任何解释或注释。"""
    
    def _default_polish_system(self) -> str:
        """默认润色 System Prompt"""
        return """你是我的首席文学翻译家，负责将初译稿润色为具有卓越文学品质的中文。

## 核心优化原则

### 语境智能净化原则
当原文中的指代、重复或衔接成分在中文语境下显得冗余或不自然时，应基于对前后文的深刻理解，进行符合中文表达习惯的简化或重组，使译文行文干净、气韵流畅。

### 诗意结构重生原则
当原文包含比喻、象征、排比等富有诗意的表达时，应深入捕捉其核心意象与情感张力，运用中文特有的凝练对仗、意象排比或古典韵律进行重构。

## 铁律（不可违反）
1. **禁止修改人名、地名、专有名词**：它们已在直译阶段被锁定。
2. **禁止改变原文的叙事视角和时态**。
3. **禁止添加原文不存在的情节或形容**。

## 风格参考
{style_reference}"""
