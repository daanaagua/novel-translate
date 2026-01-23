"""
预处理模块
负责文本清洗、章节分割、语义分块
"""
import re
from typing import List, Optional, Tuple
from pathlib import Path

from ..core.schemas import Book, Chapter, TextChunk


class TextPreprocessor:
    """文本预处理器"""
    
    # 常见章节标题模式
    CHAPTER_PATTERNS = [
        r'^Chapter\s+(\d+)',                    # Chapter 1
        r'^CHAPTER\s+(\d+)',                    # CHAPTER 1
        r'^Chapter\s+([IVXLC]+)',               # Chapter IV (罗马数字)
        r'^CHAPTER\s+([IVXLC]+)',               # CHAPTER IV
        r'^\d+\.',                              # 1.
        r'^第.+章',                              # 中文章节
    ]
    
    # 场景分隔符
    SCENE_BREAK_PATTERNS = [
        r'^\s*\*\s*\*\s*\*\s*$',                # * * *
        r'^\s*\*{3,}\s*$',                      # ***
        r'^\s*#\s*#\s*#\s*$',                   # # # #
        r'^\s*-{3,}\s*$',                       # ---
    ]
    
    def __init__(self, max_chunk_tokens: int = 1500, overlap_sentences: int = 2):
        """
        初始化预处理器
        
        Args:
            max_chunk_tokens: 每个 Chunk 的最大 Token 数（粗略估算）
            overlap_sentences: 相邻 Chunk 重叠的句子数
        """
        self.max_chunk_tokens = max_chunk_tokens
        self.overlap_sentences = overlap_sentences
    
    def load_text(self, file_path: str) -> str:
        """
        加载文本文件
        
        Args:
            file_path: 文件路径 (支持 .txt, .md)
        
        Returns:
            清洗后的文本
        """
        path = Path(file_path)
        
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        
        # 读取文件，尝试多种编码
        content = None
        for encoding in ['utf-8', 'utf-8-sig', 'gbk', 'latin-1']:
            try:
                content = path.read_text(encoding=encoding)
                break
            except UnicodeDecodeError:
                continue
        
        if content is None:
            raise ValueError(f"无法读取文件，请检查编码: {file_path}")
        
        # 基础清洗
        content = self._clean_text(content)
        return content
    
    def _clean_text(self, text: str) -> str:
        """基础文本清洗"""
        # 统一换行符
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        # 移除多余空行（保留最多两个连续换行）
        text = re.sub(r'\n{3,}', '\n\n', text)
        # 移除行首行尾空白
        lines = [line.strip() for line in text.split('\n')]
        text = '\n'.join(lines)
        return text
    
    def split_chapters(self, text: str) -> List[Tuple[str, str]]:
        """
        将文本分割为章节
        
        Args:
            text: 完整文本
        
        Returns:
            [(chapter_title, chapter_content), ...]
        """
        # 合并所有章节模式
        combined_pattern = '|'.join(f'({p})' for p in self.CHAPTER_PATTERNS)
        
        # 查找所有章节标题位置
        matches = list(re.finditer(combined_pattern, text, re.MULTILINE))
        
        if not matches:
            # 没有找到章节标题，整个文本作为一个章节
            return [("Chapter 1", text)]
        
        chapters = []
        for i, match in enumerate(matches):
            title = match.group().strip()
            start = match.end()
            
            # 确定章节结束位置
            if i + 1 < len(matches):
                end = matches[i + 1].start()
            else:
                end = len(text)
            
            content = text[start:end].strip()
            if content:  # 忽略空章节
                chapters.append((title, content))
        
        return chapters
    
    def split_into_chunks(
        self,
        text: str,
        chapter_id: str
    ) -> List[TextChunk]:
        """
        将章节文本分割为语义完整的 Chunk
        
        核心原则：
        1. 不在句子中间切断
        2. 尽量在段落边界切分
        3. 识别场景分隔符作为优先切分点
        
        Args:
            text: 章节文本
            chapter_id: 章节 ID
        
        Returns:
            TextChunk 列表
        """
        # 按段落分割
        paragraphs = self._split_paragraphs(text)
        
        chunks = []
        current_paragraphs = []
        current_tokens = 0
        chunk_index = 0
        
        for para in paragraphs:
            para_tokens = self._estimate_tokens(para)
            
            # 检查是否是场景分隔符
            is_scene_break = self._is_scene_break(para)
            
            # 判断是否需要切分
            should_split = False
            if is_scene_break and current_paragraphs:
                # 场景分隔符是最佳切分点
                should_split = True
            elif current_tokens + para_tokens > self.max_chunk_tokens and current_paragraphs:
                # 超过 Token 限制
                should_split = True
            
            if should_split:
                # 保存当前 Chunk
                chunk = TextChunk(
                    id=f"{chapter_id}_{chunk_index:03d}",
                    chapter_id=chapter_id,
                    index=chunk_index,
                    source_text='\n\n'.join(current_paragraphs),
                    token_count=current_tokens
                )
                chunks.append(chunk)
                chunk_index += 1
                
                # 重置，保留重叠部分
                if self.overlap_sentences > 0 and current_paragraphs:
                    # 从最后一段取几个句子作为重叠
                    overlap_text = self._get_last_sentences(
                        current_paragraphs[-1],
                        self.overlap_sentences
                    )
                    current_paragraphs = [overlap_text] if overlap_text else []
                    current_tokens = self._estimate_tokens(overlap_text) if overlap_text else 0
                else:
                    current_paragraphs = []
                    current_tokens = 0
            
            # 添加当前段落（除非是场景分隔符）
            if not is_scene_break:
                current_paragraphs.append(para)
                current_tokens += para_tokens
        
        # 保存最后一个 Chunk
        if current_paragraphs:
            chunk = TextChunk(
                id=f"{chapter_id}_{chunk_index:03d}",
                chapter_id=chapter_id,
                index=chunk_index,
                source_text='\n\n'.join(current_paragraphs),
                token_count=current_tokens
            )
            chunks.append(chunk)
        
        return chunks
    
    def _split_paragraphs(self, text: str) -> List[str]:
        """按段落分割文本"""
        # 双换行分割段落
        paragraphs = re.split(r'\n\s*\n', text)
        # 过滤空段落
        return [p.strip() for p in paragraphs if p.strip()]
    
    def _is_scene_break(self, text: str) -> bool:
        """判断是否是场景分隔符"""
        for pattern in self.SCENE_BREAK_PATTERNS:
            if re.match(pattern, text.strip()):
                return True
        return False
    
    def _estimate_tokens(self, text: str) -> int:
        """
        粗略估算 Token 数
        英文约 1 token/word，中文约 1.5 token/字
        """
        # 简单估算：英文按空格分词，中文按字数
        word_count = len(text.split())
        char_count = len(re.findall(r'[\u4e00-\u9fff]', text))
        return word_count + int(char_count * 1.5)
    
    def _get_last_sentences(self, text: str, n: int) -> str:
        """获取文本最后 n 个句子"""
        # 按句号分割
        sentences = re.split(r'(?<=[.!?。！？])\s+', text)
        if len(sentences) <= n:
            return text
        return ' '.join(sentences[-n:])
    
    def create_book(
        self,
        file_path: str,
        book_id: str,
        title: str
    ) -> Book:
        """
        从文件创建 Book 对象
        
        Args:
            file_path: 源文件路径
            book_id: 书籍 ID
            title: 书名
        
        Returns:
            完整的 Book 对象（包含所有章节和分块）
        """
        # 加载文本
        text = self.load_text(file_path)
        
        # 分割章节
        raw_chapters = self.split_chapters(text)
        
        # 构建 Book
        chapters = []
        for i, (chapter_title, chapter_content) in enumerate(raw_chapters):
            chapter_id = f"ch{i+1:02d}"
            
            # 分块
            chunks = self.split_into_chunks(chapter_content, chapter_id)
            
            chapter = Chapter(
                id=chapter_id,
                title=chapter_title,
                index=i,
                source_text=chapter_content,
                chunks=chunks
            )
            chapters.append(chapter)
        
        book = Book(
            id=book_id,
            title=title,
            source_file=file_path,
            chapters=chapters
        )
        
        return book
