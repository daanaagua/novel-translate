"""
数据模型定义 (Pydantic Schema)
定义系统中所有核心数据结构
"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from enum import Enum


# ============================================================
# 枚举类型
# ============================================================

class ChunkStatus(str, Enum):
    """翻译块状态"""
    PENDING = "pending"           # 待处理
    ANALYZING = "analyzing"       # 逻辑分析中
    DRAFTING = "drafting"         # 直译中
    POLISHING = "polishing"       # 润色中
    COMPLETED = "completed"       # 已完成
    FAILED = "failed"             # 失败
    HUMAN_REVIEW = "human_review" # 等待人工审核


class TermCategory(str, Enum):
    """术语分类"""
    PERSON = "person"             # 人名
    PLACE = "place"               # 地名
    ORGANIZATION = "organization" # 组织
    ITEM = "item"                 # 物品
    CONCEPT = "concept"           # 概念/术语


# ============================================================
# 术语表相关
# ============================================================

class GlossaryRule(BaseModel):
    """术语的语境规则"""
    condition: str = Field(..., description="触发条件描述，如 'respectful_address'")
    target: str = Field(..., description="该条件下的译法")
    example: Optional[str] = Field(None, description="示例")


class GlossaryItem(BaseModel):
    """术语条目"""
    id: Optional[str] = Field(None, description="唯一标识")
    src: str = Field(..., description="英文原词")
    default_target: str = Field(..., description="默认译法")
    category: Optional[TermCategory] = Field(None, description="分类")
    description: Optional[str] = Field(None, description="说明")
    rules: List[GlossaryRule] = Field(default_factory=list, description="语境规则")
    
    def to_prompt_text(self) -> str:
        """转换为可嵌入 Prompt 的文本格式"""
        lines = [f"- {self.src} -> {self.default_target}"]
        if self.description:
            lines[0] += f" ({self.description})"
        for rule in self.rules:
            lines.append(f"  - 若 {rule.condition}: 译为 「{rule.target}」")
            if rule.example:
                lines.append(f"    例: {rule.example}")
        return "\n".join(lines)


class Glossary(BaseModel):
    """完整术语表"""
    items: List[GlossaryItem] = Field(default_factory=list)
    
    def find_by_src(self, src: str) -> Optional[GlossaryItem]:
        """根据原词查找术语"""
        for item in self.items:
            if item.src.lower() == src.lower():
                return item
        return None
    
    def to_prompt_text(self) -> str:
        """转换为 Prompt 文本"""
        if not self.items:
            return "（无特殊术语）"
        return "\n".join(item.to_prompt_text() for item in self.items)


# ============================================================
# 逻辑分析相关
# ============================================================

class AmbiguityAnalysis(BaseModel):
    """单条歧义分析"""
    quote: str = Field(..., description="原文中有歧义的句子")
    connects_to: str = Field(..., description="它修饰的前文句子")
    interpretation: str = Field(..., description="中文解释实际含义")
    disconnection: Optional[str] = Field(None, description="它不是修饰哪句话")


class LogicAnalysisResult(BaseModel):
    """逻辑分析结果"""
    has_ambiguity: bool = Field(False, description="是否存在歧义")
    analysis: List[AmbiguityAnalysis] = Field(default_factory=list)
    
    def to_prompt_text(self) -> str:
        """转换为可嵌入翻译 Prompt 的提示"""
        if not self.has_ambiguity or not self.analysis:
            return "（无特殊逻辑注意事项）"
        
        lines = ["请特别注意以下逻辑关系："]
        for a in self.analysis:
            lines.append(f"- 「{a.quote}」这句话是在补充说明「{a.connects_to}」")
            lines.append(f"  含义：{a.interpretation}")
            if a.disconnection:
                lines.append(f"  注意：它不是在修饰 {a.disconnection}")
        return "\n".join(lines)


# ============================================================
# 翻译单元相关
# ============================================================

class TextChunk(BaseModel):
    """文本块"""
    id: str = Field(..., description="唯一标识，如 ch01_001")
    chapter_id: str = Field(..., description="所属章节 ID")
    index: int = Field(..., description="在章节中的序号")
    source_text: str = Field(..., description="原文")
    
    # 处理结果
    status: ChunkStatus = Field(ChunkStatus.PENDING)
    logic_analysis: Optional[LogicAnalysisResult] = None
    draft_translation: Optional[str] = None
    polished_translation: Optional[str] = None
    final_translation: Optional[str] = None
    
    # 元数据
    token_count: Optional[int] = None
    error_message: Optional[str] = None


class Chapter(BaseModel):
    """章节"""
    id: str = Field(..., description="章节 ID，如 ch01")
    title: Optional[str] = Field(None, description="章节标题")
    index: int = Field(..., description="章节序号")
    source_text: str = Field(..., description="原文")
    
    # 分块
    chunks: List[TextChunk] = Field(default_factory=list)
    
    # 上下文
    summary: Optional[str] = Field(None, description="剧情摘要")


class Book(BaseModel):
    """书籍"""
    id: str = Field(..., description="书籍 ID")
    title: str = Field(..., description="书名")
    author: Optional[str] = None
    source_file: str = Field(..., description="源文件路径")
    
    chapters: List[Chapter] = Field(default_factory=list)
    glossary: Glossary = Field(default_factory=Glossary)
    
    # 翻译进度
    current_chapter_index: int = Field(0, description="当前处理到的章节")
    current_chunk_index: int = Field(0, description="当前处理到的 Chunk")


# ============================================================
# 项目状态
# ============================================================

class ProjectState(BaseModel):
    """项目状态（用于断点续传）"""
    book_id: str
    current_chapter: int = 0
    current_chunk: int = 0
    total_chapters: int = 0
    total_chunks: int = 0
    completed_chunks: int = 0
    failed_chunks: List[str] = Field(default_factory=list)
    
    @property
    def progress_percent(self) -> float:
        """完成百分比"""
        if self.total_chunks == 0:
            return 0.0
        return (self.completed_chunks / self.total_chunks) * 100
