"""
预处理模块
负责文本清洗、章节分割、语义分块
"""
import re
from typing import List, Optional, Tuple
from pathlib import Path

from ..core.schemas import Book, Chapter, TextChunk
from ..core.epub_reader import EpubReader


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
        r'^([IVXLC]+)\s*$',                     # 纯罗马数字 (如 II, III) - 独占一行
    ]
    
    # 卷/书标题模式 (针对多卷本合集)
    VOLUME_PATTERNS = [
        r'^BOOK\s+(?:\d+|[IVXLC]+|ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN|ELEVEN|TWELVE)\s*$',
        r'^THE\s+SHADOW\s+OF\s+THE\s+TORTURER', # 第一卷
        r'^THE\s+CLAW\s+OF\s+THE\s+CONCILIATOR', # 第二卷
        r'^THE\s+SWORD\s+OF\s+THE\s+LICTOR',     # 第三卷
        r'^THE\s+CITADEL\s+OF\s+THE\s+AUTARCH',  # 第四卷
        r'^THE\s+URTH\s+OF\s+THE\s+NEW\s+SUN',   # 续集
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
        加载文本文件 (支持 .txt, .md, .docx, .epub)
        
        Args:
            file_path: 文件路径
        
        Returns:
            清洗后的文本
        """
        path = Path(file_path)
        
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
            
        suffix = path.suffix.lower()
        
        if suffix == '.docx':
            return self._load_docx(path)
        elif suffix == '.epub':
            document = EpubReader.read(path)
            return self._clean_text(EpubReader.to_chapter_marked_text(document))
        elif suffix not in {'.txt', '.md'}:
            raise ValueError(f"不支持的文件格式: {suffix}；支持 .txt、.md、.docx、.epub")
        else:
            return self._load_plain_text(path)

    def _load_docx(self, path: Path) -> str:
        """加载 docx 文件"""
        try:
            import docx
            doc = docx.Document(path)
            # 提取所有段落文本
            # 改进：直接过滤掉空段落，避免 excessive newlines 干扰正则
            full_text = []
            for para in doc.paragraphs:
                text = para.text.strip()
                if text:
                    full_text.append(text)
            
            # 用双换行连接，保持段落感
            return '\n\n'.join(full_text)
        except ImportError:
            raise ImportError("请安装 python-docx 以支持 .docx 文件: pip install python-docx")
        except Exception as e:
            raise ValueError(f"读取 docx 失败: {e}")

    def _load_plain_text(self, path: Path) -> str:
        """加载纯文本文件"""
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
    
    def split_chapters(self, text: str) -> List[Tuple[str, str, str]]:
        """
        将文本分割为章节 (支持多卷结构 + 智能兜底)
        
        Returns:
            [(chapter_id, chapter_title, chapter_content), ...]
        """
        # 1. 尝试正则切分 Volume
        volume_pattern = '|'.join(f'({p})' for p in self.VOLUME_PATTERNS)
        volume_matches = list(re.finditer(volume_pattern, text, re.MULTILINE | re.IGNORECASE))
        
        volumes = []
        if not volume_matches:
            volumes.append(("v01", text))
        else:
            if volume_matches[0].start() > 0:
                volumes.append(("v00", text[:volume_matches[0].start()]))
            
            for i, match in enumerate(volume_matches):
                vol_id = f"v{i+1:02d}"
                start = match.end()
                end = volume_matches[i+1].start() if i + 1 < len(volume_matches) else len(text)
                volumes.append((vol_id, text[start:end]))

        # 2. 在每个 Volume 内切分 Chapter
        chapters = []
        combined_chapter_pattern = '|'.join(f'({p})' for p in self.CHAPTER_PATTERNS)
        
        for vol_id, vol_content in volumes:
            # 尝试正则切分
            vol_chapters = self._regex_split_chapters(vol_content, combined_chapter_pattern, vol_id)
            
            # 检查切分结果是否合理
            # 如果内容很长（>1万字）但只切出不到 2 章，说明正则可能失效
            if len(vol_content) > 10000 and len(vol_chapters) < 2:
                print(f"[Preprocessor] 卷 {vol_id} 正则切分效果不佳 (仅 {len(vol_chapters)} 章)，启用物理兜底分章...")
                vol_chapters = self._fallback_split_chapters(vol_content, vol_id)
            
            chapters.extend(vol_chapters)
            
        return chapters

    def _regex_split_chapters(self, text: str, pattern: str, vol_id: str) -> List[Tuple[str, str, str]]:
        """正则切分逻辑"""
        matches = list(re.finditer(pattern, text, re.MULTILINE))
        if not matches:
            return [(f"{vol_id}_ch00", f"Volume {vol_id}", text)]
            
        chapters = []
        if matches[0].start() > 0:
            pre_content = text[:matches[0].start()].strip()
            if pre_content:
                chapters.append((f"{vol_id}_pre", "Preamble", pre_content))
                
        id_occurrences = {}
        for i, match in enumerate(matches):
            title = match.group().strip()
            start = match.end()
            end = matches[i+1].start() if i + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            
            if content:
                # 尝试提取数字 ID
                nums = re.findall(r'\d+', title)
                if nums:
                    idx = int(nums[0])
                else:
                    # 罗马数字转阿拉伯数字 (简化版，仅处理 I-X)
                    roman_map = {'I':1, 'II':2, 'III':3, 'IV':4, 'V':5, 'VI':6, 'VII':7, 'VIII':8, 'IX':9, 'X':10}
                    clean_title = title.replace('CHAPTER','').replace('Chapter','').strip().upper()
                    idx = roman_map.get(clean_title, i + 1)
                
                base_id = f"{vol_id}_ch{idx:02d}"
                occurrence = id_occurrences.get(base_id, 0) + 1
                id_occurrences[base_id] = occurrence
                ch_id = base_id if occurrence == 1 else f"{base_id}_{occurrence:02d}"
                chapters.append((ch_id, title, content))
        return chapters

    def _fallback_split_chapters(self, text: str, vol_id: str, chunk_size: int = 5000) -> List[Tuple[str, str, str]]:
        """
        兜底分章逻辑：按字数强制切分
        适用于无任何章节标题的文本
        """
        # 简单按双换行符分割段落
        paragraphs = text.split('\n\n')
        current_chunk = []
        current_len = 0
        chapters = []
        idx = 1
        
        for para in paragraphs:
            current_chunk.append(para)
            current_len += len(para)
            
            if current_len >= chunk_size:
                content = '\n\n'.join(current_chunk)
                chapters.append((f"{vol_id}_auto_{idx:03d}", f"Part {idx}", content))
                current_chunk = []
                current_len = 0
                idx += 1
        
        if current_chunk:
            content = '\n\n'.join(current_chunk)
            chapters.append((f"{vol_id}_auto_{idx:03d}", f"Part {idx}", content))
            
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
        """按段落分割文本，对超长段落进行硬切分"""
        # 双换行分割段落
        raw_paragraphs = re.split(r'\n\s*\n', text)
        final_paragraphs = []
        
        # 设定硬切分阈值 (字符数)
        # 英文平均单词长度5，1500 tokens 约等于 6000-8000 字符
        MAX_CHAR_PER_PARA = 3000 
        
        for p in raw_paragraphs:
            p = p.strip()
            if not p:
                continue
                
            if len(p) <= MAX_CHAR_PER_PARA:
                final_paragraphs.append(p)
            else:
                # 暴力按句号切分长段落
                sentences = re.split(r'(?<=[.!?。！？])\s+', p)
                current_chunk = []
                current_len = 0
                
                for sent in sentences:
                    current_chunk.append(sent)
                    current_len += len(sent)
                    if current_len >= MAX_CHAR_PER_PARA:
                        final_paragraphs.append(' '.join(current_chunk))
                        current_chunk = []
                        current_len = 0
                
                if current_chunk:
                    final_paragraphs.append(' '.join(current_chunk))
                    
        return final_paragraphs
    
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
        # 注意：raw_chapters 现在是 (id, title, content)
        for i, (ch_id, chapter_title, chapter_content) in enumerate(raw_chapters):
            # 分块
            chunks = self.split_into_chunks(chapter_content, ch_id)
            
            chapter = Chapter(
                id=ch_id,
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
