"""
项目管理器模块
负责管理多小说项目的目录结构、配置隔离和资源加载
"""
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any
import yaml

from .schemas import Book
from .preprocessor import TextPreprocessor
from .history import TranslationMemory
from .knowledge_base import KnowledgeBase


class Project:
    """项目实例对象"""
    def __init__(self, book_id: str, base_dir: Path):
        self.book_id = book_id
        self.root_dir = base_dir / book_id
        self.source_file = self.root_dir / "source.txt"
        self.config_file = self.root_dir / "config.yaml"
        self.glossary_dir = self.root_dir / "glossary"
        
        # 翻译记忆
        self.memory = TranslationMemory(self.root_dir)
        
        # 知识库
        self.knowledge_base = KnowledgeBase(str(self.root_dir))
        
        # 状态
        self._book_metadata: Optional[Book] = None

    @property
    def book(self) -> Book:
        """获取书籍元数据（Manifest）"""
        if self._book_metadata:
            return self._book_metadata
            
        # 尝试从 artifacts 加载 manifest
        manifest_file = self.root_dir / "artifacts" / "manifest.json"
        if manifest_file.exists():
            # 这里的 manifest 通常只包含结构信息，详细章节内容在 chapters/ 下
            # 为了性能，我们只加载基本信息，章节按需加载
            # 但为了兼容性，目前先重新解析一遍或加载完整结构
            # 简化起见：我们假设运行时重新扫描 source 或加载缓存
            pass
        
        raise RuntimeError("项目未初始化或 Manifest 丢失")

    def exists(self) -> bool:
        return self.root_dir.exists() and self.source_file.exists()


class ProjectManager:
    """
    项目管理器
    单例模式（建议），管理 projects/ 目录
    """
    
    def __init__(self, projects_root: str = "projects"):
        self.projects_root = Path(projects_root)
        self.projects_root.mkdir(parents=True, exist_ok=True)
    
    def create_project(self, book_id: str, source_path: str, force: bool = False) -> Project:
        """
        创建新项目
        
        Args:
            book_id: 书籍唯一标识 (folder name)
            source_path: 原文文件路径
            force: 是否覆盖已存在项目
        """
        project_dir = self.projects_root / book_id
        
        if project_dir.exists():
            if not force:
                raise FileExistsError(f"项目 {book_id} 已存在")
            shutil.rmtree(project_dir)
        
        # 1. 创建目录结构
        project_dir.mkdir()
        (project_dir / "glossary").mkdir()
        (project_dir / "artifacts").mkdir()
        
        # 2. 处理原文 (格式转换)
        src_file = Path(source_path)
        if not src_file.exists():
            raise FileNotFoundError(f"源文件不存在: {source_path}")
        
        dest_source = project_dir / "source.txt"
        
        # 使用 Preprocessor 加载（支持 docx 自动转换）
        # 注意：这里我们临时实例化一个 Preprocessor 仅用于转换
        temp_prep = TextPreprocessor()
        try:
            # 加载并清洗文本
            clean_text = temp_prep.load_text(str(src_file))
            # 写入项目目录为纯文本
            dest_source.write_text(clean_text, encoding='utf-8')
        except Exception as e:
            # 如果转换失败，回滚清理目录
            shutil.rmtree(project_dir)
            raise RuntimeError(f"原文处理失败: {e}")
        
        # 3. 初始化项目对象
        project = Project(book_id, self.projects_root)
        
        # 4. 执行预处理 (构建 Artifacts)
        self._initialize_artifacts(project, book_id)
        
        # 5. 初始化默认术语表 (可选，复制全局模板)
        # self._init_default_glossary(project)
        
        return project
    
    def load_project(self, book_id: str) -> Project:
        """加载已有项目"""
        project = Project(book_id, self.projects_root)
        if not project.exists():
            raise FileNotFoundError(f"项目 {book_id} 不存在")
        return project
        
    def list_projects(self) -> List[str]:
        """列出所有项目"""
        return [d.name for d in self.projects_root.iterdir() if d.is_dir()]

    def _initialize_artifacts(self, project: Project, title: str):
        """运行预处理并生成初始 artifacts"""
        print(f"正在初始化项目 {title}，进行文本预处理...")
        
        preprocessor = TextPreprocessor()
        book = preprocessor.create_book(
            file_path=str(project.source_file),
            book_id=project.book_id,
            title=title
        )
        
        # 保存 Manifest (简略版，不含全文以减小体积，这里先存完整的用于演示)
        manifest_file = project.root_dir / "artifacts" / "manifest.json"
        
        # 初始化章节文件
        for chapter in book.chapters:
            project.memory.initialize_chapter(chapter)
            
        print(f"项目初始化完成: {len(book.chapters)} 个章节")
