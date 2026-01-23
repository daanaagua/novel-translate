"""
翻译记忆模块 (Translation Memory)
负责管理翻译结果的持久化、检索和增量更新检查
"""
import json
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

from .schemas import TextChunk, ChunkStatus, Chapter


class TranslationMemory:
    """
    翻译记忆库
    管理特定项目的翻译状态和内容
    """
    
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.artifacts_dir = project_dir / "artifacts"
        self.chapters_dir = self.artifacts_dir / "chapters"
        
        # 确保目录存在
        self.chapters_dir.mkdir(parents=True, exist_ok=True)
    
    def save_chunk(self, chunk: TextChunk) -> None:
        """
        保存单个 Chunk 的翻译结果
        实际上是更新其所属的章节文件（为了保持原子性和减少文件碎片）
        """
        chapter_file = self.chapters_dir / f"{chunk.chapter_id}.json"
        
        if not chapter_file.exists():
            raise FileNotFoundError(f"章节文件不存在: {chapter_file}")
        
        # 读取章节
        chapter_data = json.loads(chapter_file.read_text(encoding='utf-8'))
        
        # 更新对应的 Chunk
        found = False
        for i, c_data in enumerate(chapter_data.get("chunks", [])):
            if c_data["id"] == chunk.id:
                # 转换为 dict 并合并（保留原有字段）
                updated_data = chunk.model_dump(mode='json')
                chapter_data["chunks"][i] = updated_data
                found = True
                break
        
        if not found:
            raise ValueError(f"Chunk {chunk.id} 不在章节文件中")
        
        # 写入文件
        # TODO: 生产环境应使用原子写入 (temp file -> rename)
        chapter_file.write_text(
            json.dumps(chapter_data, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
    
    def get_chunk(self, chapter_id: str, chunk_index: int) -> Optional[TextChunk]:
        """获取指定 Chunk"""
        chapter = self.load_chapter(chapter_id)
        if not chapter:
            return None
        
        for chunk in chapter.chunks:
            if chunk.index == chunk_index:
                return chunk
        return None

    def load_chapter(self, chapter_id: str) -> Optional[Chapter]:
        """加载完整章节"""
        chapter_file = self.chapters_dir / f"{chapter_id}.json"
        if not chapter_file.exists():
            return None
            
        try:
            data = json.loads(chapter_file.read_text(encoding='utf-8'))
            return Chapter(**data)
        except Exception as e:
            print(f"Error loading chapter {chapter_id}: {e}")
            return None

    def should_skip(self, chunk: TextChunk, force: bool = False) -> bool:
        """
        判断是否应该跳过翻译
        
        规则：
        1. 如果 force=True，不跳过
        2. 如果状态是 COMPLETED 或 HUMAN_REVIEW，且已有译文，则跳过
        """
        if force:
            return False
        
        # 只有完成且有内容的才跳过
        is_finished = chunk.status in [ChunkStatus.COMPLETED, ChunkStatus.HUMAN_REVIEW]
        has_content = bool(chunk.final_translation)
        
        return is_finished and has_content

    def initialize_chapter(self, chapter: Chapter) -> None:
        """初始化章节存储（如果不存在）"""
        chapter_file = self.chapters_dir / f"{chapter.id}.json"
        if not chapter_file.exists():
            chapter_file.write_text(
                chapter.model_dump_json(indent=2),
                encoding='utf-8'
            )

    def get_context_for_chunk(self, chunk: TextChunk, window_size: int = 2000) -> str:
        """
        获取用于翻译的上下文
        
        策略：
        1. 优先获取该 Chunk 所在章节的前文剧情摘要 (Chapter Summary)
        2. 如果没有摘要，尝试获取前几个 Chunk 的【已确认】译文作为参考
        """
        # TODO: 实现更高级的上下文检索逻辑
        # 目前简单返回上一段的译文（如果在同一章）
        
        if chunk.index == 0:
            return ""
            
        # 尝试获取上一个 chunk
        prev_chunk = self.get_chunk(chunk.chapter_id, chunk.index - 1)
        if prev_chunk and prev_chunk.final_translation:
            return f"【上文衔接】\n{prev_chunk.final_translation[-500:]}"
            
        return ""
