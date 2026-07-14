"""
翻译引擎模块
核心翻译流程：逻辑分析 -> 直译 -> 润色
"""
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from .schemas import TextChunk, ChunkStatus, TermStatus
from .llm_client import LLMManager
from ..agents.glossary_manager import GlossaryManager
from .knowledge_base import KnowledgeBase
from .text_normalization import normalize_chinese_quote_style

@dataclass
class TranslationConfig:
    """翻译配置"""
    draft_temperature: float = 0.1
    draft_max_tokens: int = 4096
    polish_temperature: float = 0.7
    polish_max_tokens: int = 4096
    enable_polish: bool = True
    style_reference: Optional[str] = None
    
    # 术语表模式: "auto" (AI可信) 或 "manual" (人工审核)
    glossary_mode: str = "auto" 
    strict_response_parsing: bool = False
    persist_discoveries: bool = True



class TranslationEngine:
    # ...
    def __init__(
        self,
        llm_manager: LLMManager,
        glossary_manager: GlossaryManager,
        knowledge_base: Optional[KnowledgeBase] = None,
        prompts: Dict[str, Any] = None,
        config: Optional[TranslationConfig] = None,
        proposal_sink: Optional[callable] = None,
    ):
        self.llm = llm_manager
        self.glossary = glossary_manager
        self.knowledge_base = knowledge_base
        self.prompts = prompts or {}
        self.config = config or TranslationConfig()
        self.proposal_sink = proposal_sink
        
        # 上下文缓存（用于滑动窗口）
        self._context_buffer: List[str] = []
        self._last_chunk_translation: Optional[str] = None
    
    def _parse_xml_response(self, text: str) -> Dict[str, Any]:
        """Parse XML-like response from LLM using regex (Robust)"""
        import re
        result = {
            "analysis": "",
            "translation": "",
            "memory_summary": "",
            "new_terms": [],
            "relations": []
        }
        
        # Helper to extract tag content
        def get_tag_content(tag, source):
            match = re.search(f"<{tag}>(.*?)</{tag}>", source, re.DOTALL | re.IGNORECASE)
            return match.group(1).strip() if match else ""

        result["analysis"] = get_tag_content("analysis", text)
        result["translation"] = get_tag_content("translation", text)
        result["memory_summary"] = get_tag_content("memory_summary", text)
        
        # Helper to extract attributes order-independently
        def extract_attributes(tag_string):
            attrs = {}
            for match in re.finditer(r'(\w+)=["\'](.*?)["\']', tag_string):
                attrs[match.group(1).lower()] = match.group(2)
            return attrs

        # Extract terms
        # Find all <term ... /> or <term ...>
        for match in re.finditer(r'<term\s+(.*?)/>', text, re.DOTALL | re.IGNORECASE):
            attrs = extract_attributes(match.group(1))
            if "src" in attrs:
                result["new_terms"].append({
                    "src": attrs.get("src"),
                    "tgt": attrs.get("tgt"),
                    "type": attrs.get("type"),
                    "context": attrs.get("context")
                })
            
        # Extract relations
        for match in re.finditer(r'<relation\s+(.*?)/>', text, re.DOTALL | re.IGNORECASE):
            attrs = extract_attributes(match.group(1))
            if "sub" in attrs:
                result["relations"].append({
                    "sub": attrs.get("sub"),
                    "rel": attrs.get("rel"),
                    "obj": attrs.get("obj"),
                    "context": attrs.get("context")
                })
            
        return result

    def _sanitize_json(self, raw_json: str) -> str:
        """
        修复 LLM 输出的破损 JSON
        """
        text = raw_json.strip()
        
        # 1. 去除 markdown 代码块
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        
        text = text.strip()
        
        # 2. 修复字符串内的物理换行
        # 遍历字符串，维护 in_string 状态
        result = []
        in_string = False
        escape = False
        
        for char in text:
            if char == '"' and not escape:
                in_string = not in_string
            
            if char == '\\':
                escape = not escape
            else:
                escape = False
                
            if char == '\n' and in_string:
                result.append('\\n')
            elif char == '\t' and in_string:
                result.append('\\t')
            else:
                result.append(char)
        
        return "".join(result)

    def translate_chunk(
        self,
        chunk: TextChunk,
        memory_context: Optional[str] = None,
        previous_summary: Optional[str] = None,
        previous_chunk_text: Optional[str] = None,
        stream_callback: Optional[callable] = None,
        comparison_reference: Optional[str] = None,
    ) -> TextChunk:
        """
        翻译单个文本块 (新版流式逻辑)
        
        Args:
            stream_callback: (phase, content) -> None
        """
        try:
            chunk.error_message = None
            chunk.quality_warnings = []
            chunk.polish_retry_count = 0

            # 步骤 1: 直译 (含 Thinking & Term Extraction)
            chunk.status = ChunkStatus.DRAFTING
            if stream_callback: stream_callback("draft_start", "")
            
            # 准备 Prompt 上下文
            draft_response_generator = self._draft_translate(
                source_text=chunk.source_text,
                memory_context=memory_context or previous_summary,
                previous_chunk_text=previous_chunk_text
            )
            
            # 流式解析 Accumulator
            full_response_text = ""
            current_analysis = ""
            current_translation = ""
            
            # 简单的流式解析状态机
            # 注意：DeepSeek 等模型输出 JSON 时，可能无法完美流式解析结构，
            # 这里我们先累积全文，同时尝试提取 content 打印给用户
            
            for item in draft_response_generator:
                if isinstance(item, tuple):
                    msg_type, content = item
                else:
                    msg_type, content = "content", item
                
                if msg_type == "content":
                    full_response_text += content
                    
                    # 尝试实时回显（这是一个简化的 hack，假设模型按顺序输出 keys）
                    # 实际生产中建议使用专门的 JSON stream parser
                    if stream_callback: 
                        stream_callback("draft_content", content)

            # 解析最终响应 (优先 XML, 失败尝试 JSON)
            try:
                # 尝试 XML 解析
                if "<response>" in full_response_text:
                    response_data = self._parse_xml_response(full_response_text)
                else:
                    # 尝试修复 JSON
                    json_str = self._sanitize_json(full_response_text)
                    import json
                    response_data = json.loads(json_str)
                    
                    # Normalize keys for JSON
                    if "thinking" in response_data and "analysis" not in response_data:
                        response_data["analysis"] = response_data["thinking"]
                
                chunk.analysis = response_data.get("analysis", "")
                chunk.draft_translation = response_data.get("translation", "")
                chunk.memory_summary = response_data.get("memory_summary", "")
                
                # 处理新术语
                new_terms = response_data.get("new_terms", [])
                self._process_new_terms(new_terms, stream_callback)
                
                # 处理实体关系 (Phase 2)
                relations = response_data.get("relations", [])
                if relations:
                    self._process_relations(relations, stream_callback)
                
            except Exception as e:
                if self.config.strict_response_parsing:
                    raise ValueError(f"第一层结构化响应解析失败: {e}") from e
                # Fallback: 如果解析失败，假设全文都是译文
                print(f"[Warn] Parse failed: {e}")
                chunk.draft_translation = full_response_text
                chunk.analysis = "Parse Failed"

            draft_problems = self._translation_shape_problems(
                chunk.source_text,
                chunk.draft_translation or "",
                stage="第一层译稿",
                min_length_ratio=0.15,
            )
            if draft_problems:
                raise ValueError("；".join(draft_problems))

            # 步骤 2: 润色 (可选)
            needs_human_review = False
            if self.config.enable_polish:
                chunk.status = ChunkStatus.POLISHING
                if stream_callback: stream_callback("polish_start", "")
                
                polish_gen = self._polish_translate(
                    source_text=chunk.source_text,
                    draft_translation=chunk.draft_translation,
                    analysis=chunk.analysis or "",
                    memory_context=memory_context or previous_summary or "",
                    comparison_reference=comparison_reference or "",
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
                
                chunk.polished_translation = self._clean_final_translation(full_polish)
                polish_problems = self._translation_shape_problems(
                    chunk.draft_translation or "",
                    chunk.polished_translation or "",
                    stage="第二层定稿",
                    min_length_ratio=0.75,
                )

                if polish_problems:
                    chunk.quality_warnings.append(
                        "第一次润色未通过完整性校验：" + "；".join(polish_problems)
                    )
                    chunk.polish_retry_count = 1
                    if stream_callback:
                        stream_callback("polish_retry", "")

                    retry_gen = self._polish_translate(
                        source_text=chunk.source_text,
                        draft_translation=chunk.draft_translation or "",
                        analysis=chunk.analysis or "",
                        memory_context=memory_context or previous_summary or "",
                        comparison_reference=comparison_reference or "",
                        retry_reason="；".join(polish_problems),
                    )
                    retry_text = ""
                    for item in retry_gen:
                        if isinstance(item, tuple):
                            msg_type, content = item
                        else:
                            msg_type, content = "content", item
                        if msg_type == "content":
                            retry_text += content
                        if stream_callback:
                            stream_callback(f"polish_retry_{msg_type}", content)

                    retry_polish = self._clean_final_translation(retry_text)
                    retry_problems = self._translation_shape_problems(
                        chunk.draft_translation or "",
                        retry_polish,
                        stage="第二层重试定稿",
                        min_length_ratio=0.75,
                    )
                    chunk.polished_translation = retry_polish
                    if retry_problems:
                        chunk.quality_warnings.append(
                            "润色重试仍未通过，最终译文已回退到完整初稿："
                            + "；".join(retry_problems)
                        )
                        chunk.final_translation = chunk.draft_translation
                        needs_human_review = True
                    else:
                        chunk.final_translation = retry_polish
                else:
                    chunk.final_translation = chunk.polished_translation
            else:
                chunk.final_translation = chunk.draft_translation

            chunk.final_translation = self._normalize_chinese_punctuation(
                chunk.final_translation or ""
            )
            
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
            
            final_problems = self._translation_shape_problems(
                chunk.draft_translation or "",
                chunk.final_translation or "",
                stage="最终译文",
                min_length_ratio=0.75,
            )
            if final_problems:
                raise ValueError("；".join(final_problems))

            chunk.status = (
                ChunkStatus.HUMAN_REVIEW if needs_human_review else ChunkStatus.COMPLETED
            )
            
            # 更新上下文缓存
            self._last_chunk_translation = chunk.final_translation
            
        except Exception as e:
            chunk.status = ChunkStatus.FAILED
            chunk.error_message = str(e)
            # 重新抛出以便上层知晓
            # raise e 
            # 暂时不抛出，让流程继续
        
        return chunk

    @staticmethod
    def _translation_shape_problems(
        reference: str,
        candidate: str,
        stage: str,
        min_length_ratio: float,
    ) -> List[str]:
        """检查模型是否漏掉整段或在输出中途截断。"""
        import re

        def paragraphs(text: str) -> List[str]:
            return [
                part.strip()
                for part in re.split(r"\n\s*\n", (text or "").strip())
                if part.strip()
            ]

        def compact_length(text: str) -> int:
            return len(re.sub(r"\s+", "", text or ""))

        problems = []
        reference_paragraphs = paragraphs(reference)
        candidate_paragraphs = paragraphs(candidate)
        reference_length = compact_length(reference)
        candidate_length = compact_length(candidate)
        normalized_reference = (reference or "").strip().lower()
        # EPUB 后记中的书目信息常把同一条引文拆成多行；中文合并这些短行
        # 是正常排版，不应被当作漏段。
        allows_bibliography_merges = (
            normalized_reference.startswith("afterword")
            and normalized_reference.endswith("the end")
        )

        if not candidate_length:
            return [f"{stage}为空"]
        if (
            len(candidate_paragraphs) != len(reference_paragraphs)
            and not allows_bibliography_merges
        ):
            problems.append(
                f"{stage}段落数为{len(candidate_paragraphs)}，应为{len(reference_paragraphs)}"
            )
        if reference_length and candidate_length / reference_length < min_length_ratio:
            problems.append(
                f"{stage}长度仅为参照文本的{candidate_length / reference_length:.1%}"
            )
        return problems

    @staticmethod
    def _clean_final_translation(text: str) -> str:
        """Remove harmless wrappers while preserving the translated paragraphs."""
        import re

        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:text|markdown)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        match = re.search(
            r"<final_translation>(.*?)</final_translation>",
            cleaned,
            re.DOTALL | re.IGNORECASE,
        )
        return (match.group(1) if match else cleaned).strip()

    @staticmethod
    def _normalize_chinese_punctuation(text: str) -> str:
        """统一项目要求的中文引号，不改动普通ASCII字符。"""
        return normalize_chinese_quote_style(text)
    
    def _process_relations(self, relations: List[dict], callback: Optional[callable]):
        """处理新发现的实体关系"""
        if self.proposal_sink:
            for relation in relations:
                self.proposal_sink("relation", dict(relation))
        if not self.config.persist_discoveries:
            return
        if not self.knowledge_base:
            return

        for rel in relations:
            try:
                sub = rel.get("sub") or rel.get("subject")
                obj = rel.get("obj") or rel.get("object")
                relation = rel.get("rel") or rel.get("relation")
                context = rel.get("context")
                
                if sub and obj and relation:
                    self.knowledge_base.update_entity_relation(sub, obj, relation, context)
                    if callback:
                        callback("logic", f"[Relation] {sub} --{relation}--> {obj}")
            except Exception as e:
                print(f"[Warn] Relation update failed: {e}")

    def _process_new_terms(self, new_terms: List[dict], callback: Optional[callable]):
        """处理新发现的术语"""
        for term in new_terms:
            try:
                if self.proposal_sink:
                    self.proposal_sink("term", dict(term))
                if not self.config.persist_discoveries:
                    continue
                src = term.get("src")
                tgt = term.get("tgt", src) # 默认译名
                raw_type = term.get("type", "concept")
                context = term.get("context")
                
                if not src: continue
                
                # Normalize category (handle 'person/group', 'mythical figure' etc)
                raw_type_lower = raw_type.lower()
                # Remove brackets and split by slash
                cat_str = raw_type_lower.split("(")[0].strip().split("/")[0].strip()
                
                # Category Mapping
                type_mapping = {
                    "mythical figure": "person",
                    "mythical creature": "concept", 
                    "animal": "concept",
                    "weapon": "item",
                    "vehicle": "item",
                    "unit": "unit",
                    "description": "concept",
                    "identity": "concept",
                    "group": "organization"
                }
                
                if raw_type_lower in type_mapping:
                    cat_str = type_mapping[raw_type_lower]
                elif cat_str in type_mapping:
                    cat_str = type_mapping[cat_str]
                
                # Try to validate/convert to Enum, fallback to concept
                try:
                    # Check if valid enum value
                    from .schemas import TermCategory
                    TermCategory(cat_str)
                except ValueError:
                    cat_str = "concept"
                
                # 检查是否存在
                existing_item = self.glossary.find_by_src(src)
                if existing_item:
                    # 如果译名不同，处理更新逻辑
                    if existing_item.default_target != tgt:
                        is_auto = self.config.glossary_mode == "auto"
                        note_prefix = "[Auto-Update]" if is_auto else "[Pending Suggestion]"
                        note = f"\n{note_prefix} Alt: {tgt} (Ctx: {context or 'None'})"
                        
                        # 避免重复
                        if existing_item.description and note.strip() in existing_item.description:
                            pass
                        else:
                            existing_item.description = (existing_item.description or "") + note
                            
                            # Auto模式下更新默认译名
                            if is_auto:
                                existing_item.default_target = tgt
                                existing_item.status = TermStatus.VERIFIED
                                if callback: callback("logic", f"[Update] {src}: {existing_item.default_target} -> {tgt}")
                            else:
                                if callback: callback("logic", f"[Suggest] {src}: {tgt} (Pending)")
                                
                    continue
                    
                # 根据模式决定状态
                status = TermStatus.VERIFIED if self.config.glossary_mode == "auto" else TermStatus.PENDING
                
                description = context or "Auto-extracted"
                
                self.glossary.add_term(
                    src=src,
                    default_target=tgt,
                    category=cat_str,
                    description=description,
                    status=status
                )
                
                # Sync with KnowledgeBase
                if self.knowledge_base:
                    try:
                        entity = self.knowledge_base.add_entity(
                            name=src,
                            category=cat_str,
                            description=description
                        )
                        # Add translation as alias
                        if tgt and tgt != src:
                            self.knowledge_base.add_alias(entity.id, tgt)
                    except Exception as e:
                        print(f"[Warn] KB sync failed for {src}: {e}")
                
                if callback:
                    tag = "[New Term]" if status == TermStatus.VERIFIED else "[Pending Term]"
                    callback("logic", f"{tag} {src} -> {tgt}")
                    
            except Exception as e:
                print(f"[Warn] Term processing failed for {term}: {e}")

    def _draft_translate(
        self,
        source_text: str,
        memory_context: Optional[str] = None,
        previous_chunk_text: Optional[str] = None,
        logic_analysis: Any = None # Keep for compatibility signature, unused
    ) -> Any:
        """
        直译阶段 (Logic-Aware + Memory-Augmented)
        """
        # 1. 获取术语。已确认术语是硬约束；待审核术语只作为一致性建议。
        verified_terms = self.glossary.find_terms_in_text(
            source_text,
            status_filter=[TermStatus.VERIFIED],
        )
        pending_terms = self.glossary.find_terms_in_text(
            source_text,
            status_filter=[TermStatus.PENDING],
        )
        glossary_terms = verified_terms + pending_terms
        glossary_parts = []
        if verified_terms:
            glossary_parts.append(
                "【硬性术语】\n" + "\n".join(term.to_prompt_text() for term in verified_terms)
            )
        if pending_terms:
            glossary_parts.append(
                "【待审核建议：仅用于保持暂时一致，不得压过本段语境】\n"
                + "\n".join(term.to_prompt_text() for term in pending_terms)
            )
        glossary_rules = "\n\n".join(glossary_parts) or "（无特殊术语）"
        
        # 2. 获取实体档案 (Memory Injection)
        entity_context = ""
        if self.knowledge_base and glossary_terms:
            # 提取名字
            names = [term.src for term in glossary_terms]
            entity_context = self.knowledge_base.get_prompt_text(names)
        
        # 构建 System Prompt
        system_template = self.prompts.get("draft", {}).get("system", self._default_draft_system())
        system_prompt = system_template.format(
            glossary_rules=glossary_rules,
            entity_context=entity_context
        )
        
        # 构建 User Prompt (采用结构化 Context 注入)
        user_content = ""
        
        # 1. 注入全书滚动摘要和最近块衔接
        if memory_context:
            user_content += f"<memory_context>\n{memory_context}\n</memory_context>\n\n"
            
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
        draft_translation: str,
        analysis: str = "",
        memory_context: str = "",
        comparison_reference: str = "",
        retry_reason: str = "",
    ) -> Any:
        """
        润色阶段
        Returns: 文本 (str) 或 生成器
        """
        # 构建 System Prompt
        system_template = self.prompts.get("polish", {}).get("system", self._default_polish_system())
        verified_terms = self.glossary.find_terms_in_text(
            source_text,
            status_filter=[TermStatus.VERIFIED],
        )
        pending_terms = self.glossary.find_terms_in_text(
            source_text,
            status_filter=[TermStatus.PENDING],
        )
        glossary_parts = []
        if verified_terms:
            glossary_parts.append(
                "【硬性术语】\n" + "\n".join(term.to_prompt_text() for term in verified_terms)
            )
        if pending_terms:
            glossary_parts.append(
                "【待审核建议】\n" + "\n".join(term.to_prompt_text() for term in pending_terms)
            )
        glossary_rules = "\n\n".join(glossary_parts) or "（无特殊术语）"
        system_prompt = system_template.format(
            style_reference=self.config.style_reference or "(无特定风格参考)",
            glossary_rules=glossary_rules,
        )
        
        # 构建 User Prompt
        user_template = self.prompts.get("polish", {}).get("user", 
            "## 原文\n{source_text}\n\n## 初译稿\n{draft_translation}"
            "\n\n## 旧译文对照\n{comparison_reference}"
            "\n\n英文原文是唯一事实依据。旧译文准确且自然的句子应原样保留；"
            "只有能指出具体缺陷时才局部修改，不得为了替换同义词而重写。"
            "请输出最终译文。")
        user_prompt = user_template.format(
            source_text=source_text,
            draft_translation=draft_translation,
            analysis=analysis,
            memory_context=memory_context,
            comparison_reference=comparison_reference or "（无旧译文对照）",
        )
        if retry_reason:
            paragraph_count = len([
                part for part in __import__("re").split(
                    r"\n\s*\n", draft_translation.strip()
                ) if part.strip()
            ])
            user_prompt += (
                "\n\n## 完整性修复指令\n"
                f"上一次输出未通过完整性校验：{retry_reason}。"
                f"本次必须完整覆盖初译稿的全部{paragraph_count}个段落，段落数必须一致；"
                "不得摘要、跳段、合并段落或在中途停止。请从第一段重新输出到最后一段。"
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
        enable_polish: bool = True
    ) -> Dict[str, Any]:
        """
        翻译单段文本（便捷方法）
        """
        # 为了兼容便捷调用，我们需要构造一个 Fake Chunk
        chunk = TextChunk(
            id="temp", 
            chapter_id="temp", 
            index=0, 
            source_text=source_text
        )
        
        # 复用核心逻辑
        result_chunk = self.translate_chunk(chunk)
        
        return {
            "source": source_text,
            "analysis": result_chunk.analysis,
            "draft": result_chunk.draft_translation,
            "final": result_chunk.final_translation
        }
    
    def _default_draft_system(self) -> str:
        """默认直译 System Prompt (XML Mode)"""
        return """你是一位擅长高难度科幻文学的中文译者。你的任务是完成忠实、可校订的第一层译稿。

## 核心指令
1. **术语一致**：严格遵守提供的术语表规则。
2. **逻辑准确**：识别从句依附、指代、比较对象、观察者视角和因果关系，不得把尚未揭示的信息擅自讲明。
3. **科学准确**：物理概念、计量单位、空间关系和人造术语优先于辞藻；不确定处在 analysis 中简短注明。
4. **记忆更新**：根据旧摘要和本段，输出一份不超过1200个汉字、可以独立替代旧摘要的全书滚动摘要。只记录已经明示的事实、人物状态、概念定义和未解决问题。
5. **格式严格**：必须输出合法的 XML 格式。

## 术语表 (仅参考)
{glossary_rules}

{entity_context}

## 输出格式
请严格按照以下 XML 格式输出：

<response>
    <analysis>
    这里写下你的思考过程。1. 分析难句结构... 2. 确认代词指代... 3. 决定意译策略...
    </analysis>
    
    <translation>
    忠实、通顺的第一层中文译文（保持原文分段）
    </translation>

    <memory_summary>
    翻译本段之后更新的自包含全书摘要，不超过1200个汉字
    </memory_summary>
    
    <new_terms>
        <term src="新术语原文" tgt="建议译名" type="person/place/item/organization" />
    </new_terms>
    
    <relations>
        <relation sub="主体名" rel="关系描述" obj="客体名" context="简短证据" />
    </relations>
</response>"""
    
    def _default_polish_system(self) -> str:
        """默认润色 System Prompt"""
        return """你是第二层中文定稿译者。请逐句对照英文原文、第一层译稿和分析提示，输出准确而自然的最终译文。

## 核心优化原则

1. 先纠正漏译、误译、指代错误、逻辑关系错误和物理概念错误，再改善中文节奏。
2. 保留原文的信息释放顺序、段落、叙事距离与不确定性；不得替作者解释谜底。
3. 科学术语和人造概念保持精确、一致；不能为了顺口改写成含义更宽泛的词。
4. 中文应清楚、克制、有文学节奏，避免无依据的古雅化、诗化和增添形容词。
5. 外层引语统一使用“……”，嵌套引语使用‘……’，不得使用「……」或『……』。
6. 若提供旧译文对照，英文原文仍是唯一事实依据；旧译文准确且自然的句子应原样保留，只有能从原文指出具体缺陷时才局部修改。
7. 不得为了替换同义词或统一个人语气而重写正确句子；每处修改都必须检查主宾语、代词指向、否定范围和因果关系。

## 铁律（不可违反）
1. **禁止修改人名、地名、专有名词**：它们已在直译阶段被锁定。
2. **禁止改变原文的叙事视角和时态**。
3. **禁止添加原文不存在的情节或形容**。
4. **只输出最终中文译文**，不得附带说明、标题、XML标签或Markdown代码块。

## 已确认术语
{glossary_rules}

## 风格参考
{style_reference}"""
