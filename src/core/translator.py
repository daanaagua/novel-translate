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
from ..agents.term_extractor import TermExtractor

@dataclass
class TranslationConfig:
    """翻译配置"""
    # ... 原有字段 ...
    draft_temperature: float = 0.1
    draft_max_tokens: int = 4096
    polish_temperature: float = 0.7
    polish_max_tokens: int = 4096
    enable_logic_analysis: bool = True
    enable_polish: bool = True
    style_reference: Optional[str] = None
    
    # 新增：自动术语提取
    enable_auto_glossary: bool = False


class TranslationEngine:
    # ...
    def __init__(
        self,
        llm_manager: LLMManager,
        glossary_manager: GlossaryManager,
        prompts: Dict[str, Any],
        config: Optional[TranslationConfig] = None
    ):
        self.llm = llm_manager
        self.glossary = glossary_manager
        self.prompts = prompts
        self.config = config or TranslationConfig()
        
        self.logic_analyzer = LogicAnalyzer(
            llm_manager=llm_manager,
            prompts=prompts.get("logic_analysis", {})
        )
        self.term_extractor = TermExtractor(llm_manager)
        
        # 上下文缓存（用于滑动窗口）
        self._context_buffer: List[str] = []
        self._last_chunk_translation: Optional[str] = None
    
    def translate_chunk(
        self,
        chunk: TextChunk,
        previous_summary: Optional[str] = None,
        previous_chunk_text: Optional[str] = None,
        stream_callback: Optional[callable] = None
    ) -> TextChunk:
        """
        翻译单个文本块
        
        Args:
            stream_callback: (phase, content) -> None, 用于实时回显流式内容
        """
        try:
            # 步骤 0: 自动术语提取 (Pre-flight)
            if self.config.enable_auto_glossary:
                if stream_callback: stream_callback("logic", "正在扫描新术语...")
                new_terms = self.term_extractor.extract(chunk.source_text)
                for term in new_terms:
                    # 只有当术语不在库中时才添加
                    if not self.glossary.find_by_src(term["src"]):
                        self.glossary.add_term(
                            src=term["src"],
                            default_target=term["src"], # 默认译名暂为原文，待人工确认
                            category=term.get("category"),
                            description=f"Auto-extracted from context: {term.get('context')}"
                        )
                        if stream_callback: stream_callback("logic", f"发现新术语: {term['src']}")

            # 步骤 1: 逻辑分析
            if self.config.enable_logic_analysis:
                chunk.status = ChunkStatus.ANALYZING
                if stream_callback: stream_callback("logic", "正在进行逻辑分析...")
                chunk = self.logic_analyzer.analyze_chunk(chunk)
            
            # 步骤 2: 直译
            chunk.status = ChunkStatus.DRAFTING
            if stream_callback: stream_callback("draft_start", "")
            
            draft_gen = self._draft_translate(
                source_text=chunk.source_text,
                logic_analysis=chunk.logic_analysis,
                previous_summary=previous_summary,
                previous_chunk_text=previous_chunk_text
            )
            
            # 消费直译流
            full_draft = ""
            for item in draft_gen:
                if isinstance(item, tuple):
                    msg_type, content = item
                else:
                    msg_type, content = "content", item # 兼容旧格式
                
                if msg_type == "content":
                    full_draft += content
                
                if stream_callback: stream_callback(f"draft_{msg_type}", content)
            
            chunk.draft_translation = full_draft
            
            # 步骤 3: 润色
            if self.config.enable_polish:
                chunk.status = ChunkStatus.POLISHING
                if stream_callback: stream_callback("polish_start", "")
                
                polish_gen = self._polish_translate(
                    source_text=chunk.source_text,
                    draft_translation=chunk.draft_translation
                )
                
                # 消费润色流
                full_polish = ""
                for item in polish_gen:
                    if isinstance(item, tuple):
                        msg_type, content = item
                    else:
                        msg_type, content = "content", item
                    
                    if msg_type == "content":
                        full_polish += content
                        
                    if stream_callback: stream_callback(f"polish_{msg_type}", content)
                
                chunk.polished_translation = full_polish
                chunk.final_translation = chunk.polished_translation
            else:
                chunk.final_translation = chunk.draft_translation
            
            # 去重处理：如果译文开头包含了上一段译文的末尾，则切除
            if self._last_chunk_translation:
                # 取上一段译文的最后 20 个字符
                suffix = self._last_chunk_translation[-20:].strip()
                if suffix and suffix in chunk.final_translation[:100]:
                    # 如果在当前译文前 100 字内发现了上一段的结尾，说明有重复
                    idx = chunk.final_translation.find(suffix)
                    if idx != -1:
                        # 切除重复部分
                        clean_translation = chunk.final_translation[idx + len(suffix):].strip()
                        if clean_translation: # 确保没切空
                            chunk.final_translation = clean_translation
            
            chunk.status = ChunkStatus.COMPLETED
            
            # 更新上下文缓存
            self._last_chunk_translation = chunk.final_translation
            
        except Exception as e:
            chunk.status = ChunkStatus.FAILED
            chunk.error_message = str(e)
            # 重新抛出以便上层知晓
            # raise e 
            # 暂时不抛出，让流程继续
        
        return chunk
    
    def _draft_translate(
        self,
        source_text: str,
        logic_analysis: Optional[LogicAnalysisResult] = None,
        previous_summary: Optional[str] = None,
        previous_chunk_text: Optional[str] = None
    ) -> Any: # Return type changed to Any to support stream generator or str
        """
        直译阶段
        
        Args:
            source_text: 原文
            logic_analysis: 逻辑分析结果
            previous_summary: 前文摘要 (Story Context)
            previous_chunk_text: 上一个 Chunk 的原文末尾 (Text Context)
        
        Returns:
            直译文本 (str) 或 生成器 (stream)
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
        
        # 构建 User Prompt (采用结构化 Context 注入)
        user_content = ""
        
        # 1. 注入 Story Context (摘要)
        if previous_summary:
            user_content += f"<story_context>\n{previous_summary}\n</story_context>\n\n"
            
        # 2. 注入 Text Context (上一段原文，防止割裂)
        if previous_chunk_text:
            # 取最后 200 字符作为衔接参考
            context_snippet = previous_chunk_text[-200:] if len(previous_chunk_text) > 200 else previous_chunk_text
            user_content += f"<immediate_context>\n...{context_snippet}\n</immediate_context>\n\n"
        
        # 3. 注入当前文本
        user_content += f"<text_to_translate>\n{source_text}\n</text_to_translate>"
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
        
        # 强制流式输出
        return self.llm.chat(
            messages=messages,
            purpose="draft",
            temperature=self.config.draft_temperature,
            max_tokens=self.config.draft_max_tokens,
            stream=True # Enable streaming
        )
    
    def _polish_translate(
        self,
        source_text: str,
        draft_translation: str
    ) -> Any:
        """
        润色阶段
        Returns: 文本 (str) 或 生成器
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
            max_tokens=self.config.polish_max_tokens,
            stream=True # Force streaming
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

## 核心指令
1. **只翻译 <text_to_translate> 标签内的内容**。
2. `<story_context>` 和 `<immediate_context>` 仅作为理解参考，**绝对不要翻译它们**。
3. **准确第一**：绝对忠于原文的事实和逻辑，不添加不删减。
4. **术语一致**：严格遵守提供的术语表规则。
5. **逻辑遵从**：如果提供了逻辑分析，必须按照分析结果处理指代关系。

## 术语表
{glossary_rules}

## 逻辑分析
{logic_analysis}

## 输出要求
- 直接输出中文译文，保持原文的分段格式。
- 不要输出标签，也不要添加任何解释。"""
    
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
