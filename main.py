"""
DeepNovel-Translator CLI 入口 (v2.0 Project-Based)
"""
import argparse
import json
import sys
import shutil
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from src.utils.config_loader import ConfigLoader
from src.core.llm_client import LLMManager
from src.core.translator import TranslationEngine, TranslationConfig
from src.core.project_manager import ProjectManager
from src.agents.glossary_manager import GlossaryManager
from src.core.schemas import ChunkStatus
from rich.console import Console
from rich.theme import Theme

# 初始化 Rich Console
console = Console(theme=Theme({"think": "dim white", "info": "cyan"}))

# 初始化核心组件
config_loader = ConfigLoader(config_dir=str(project_root / "config"))
project_manager = ProjectManager(projects_root=str(project_root / "projects"))

def stream_printer(phase, content):
    """流式打印回调"""
    if phase == "logic":
        console.print(f"\r[info][Logic][/info] {content}", end="")
    elif phase == "draft_start":
        console.print("\n\n[bold green][Draft][/bold green] ", end="")
    elif phase == "draft_think":
        console.print(content, style="think", end="")
    elif phase == "draft_content":
        console.print(content, end="")
    elif phase == "polish_start":
        console.print("\n\n[bold yellow][Polish][/bold yellow] ", end="")
    elif phase == "polish_think":
        console.print(content, style="think", end="")
    elif phase == "polish_content":
        console.print(content, end="")

def cmd_init(args):
    """初始化新项目"""
    print(f"正在创建项目: {args.book_id} ...")
    try:
        project = project_manager.create_project(
            book_id=args.book_id,
            source_path=args.file,
            force=args.force
        )
        print(f"[OK] 项目创建成功！位置: {project.root_dir}")
        print(f"下一步: 请运行 'python main.py translate {args.book_id}' 开始翻译")
    except Exception as e:
        print(f"[ERROR] 创建失败: {e}")

def cmd_list(args):
    """列出所有项目"""
    projects = project_manager.list_projects()
    if not projects:
        print("暂无项目。请使用 'init' 命令创建。")
        return
    
    print("【项目列表】")
    for p in projects:
        print(f"  - {p}")

def cmd_translate(args):
    """项目翻译命令"""
    book_id = args.book_id
    
    try:
        project = project_manager.load_project(book_id)
    except FileNotFoundError:
        print(f"❌ 项目 '{book_id}' 不存在。")
        return

    # 1. 加载配置
    global_config = config_loader.load_config()
    prompts = config_loader.load_prompts()
    
    # 2. 初始化引擎组件
    llm_manager = LLMManager(global_config["llm"])
    
    # 加载项目专用术语表
    glossary_manager = GlossaryManager(str(project.glossary_dir))
    glossary_manager.load()
    
    # 初始化翻译引擎
    trans_config = TranslationConfig(
        enable_polish=not args.no_polish,
        draft_temperature=args.temp_draft,
        polish_temperature=args.temp_polish,
        glossary_mode=args.glossary_mode
    )
    engine = TranslationEngine(
        llm_manager=llm_manager,
        glossary_manager=glossary_manager,
        knowledge_base=project.knowledge_base,
        prompts=prompts,
        config=trans_config
    )
    
    print(f"[START] 开始翻译项目: {book_id}")
    print(f"配置: 术语模式={args.glossary_mode}, 润色={'OFF' if args.no_polish else 'ON'}")
    
    # 3. 遍历章节和 Chunk
    chapter_files = sorted(project.memory.chapters_dir.glob("*.json"))
    
    total_processed = 0
    total_skipped = 0
    
    for ch_file in chapter_files:
        chapter_id = ch_file.stem
        
        # 如果指定了章节范围
        if args.chapters and chapter_id not in args.chapters.split(','):
            continue
            
        print(f"\n[Chapter] 处理章节: {chapter_id}")
        
        # 加载章节数据
        chapter = project.memory.load_chapter(chapter_id)
        if not chapter:
            print(f"  [WARN] 无法加载章节 {chapter_id}")
            continue
            
        for chunk in chapter.chunks:
            # 检查是否需要跳过
            if project.memory.should_skip(chunk, force=args.force):
                print(f"  [SKIP] Chunk {chunk.id}: 已完成，跳过")
                total_skipped += 1
                continue
            
            print(f"  [TRANS] 翻译 Chunk {chunk.id} (Tokens: {chunk.token_count})...")
            
            # 获取上下文
            context = project.memory.get_context_for_chunk(chunk)
            
            # 获取上一段原文 (Text Context)
            prev_chunk_obj = project.memory.get_previous_chunk(chunk)
            prev_source = prev_chunk_obj.source_text if prev_chunk_obj else None
            
            # 执行翻译 (带流式回调)
            result_chunk = engine.translate_chunk(
                chunk, 
                previous_summary=context,
                previous_chunk_text=prev_source,
                stream_callback=stream_printer
            )
            
            print("") # 换行
            
            # 保存结果
            project.memory.save_chunk(result_chunk)
            
            if result_chunk.status == ChunkStatus.COMPLETED:
                print(f"     [OK] 完成")
            else:
                print(f"     [ERR] 失败: {result_chunk.error_message}")
            
            total_processed += 1
            
    print("\n" + "="*40)
    print(f"[DONE] 任务结束")
    print(f"处理: {total_processed}")
    print(f"跳过: {total_skipped}")

def main():
    parser = argparse.ArgumentParser(
        description="DeepNovel-Translator CLI (v2.0)",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")
    
    # init
    p_init = subparsers.add_parser("init", help="初始化新项目")
    p_init.add_argument("book_id", help="项目ID (英文，无空格)")
    p_init.add_argument("file", help="小说原文路径 (.txt)")
    p_init.add_argument("--force", "-f", action="store_true", help="强制覆盖已存在项目")
    p_init.set_defaults(func=cmd_init)
    
    # list
    p_list = subparsers.add_parser("list", help="列出所有项目")
    p_list.set_defaults(func=cmd_list)
    
    # translate
    p_trans = subparsers.add_parser("translate", help="翻译项目")
    p_trans.add_argument("book_id", help="项目ID")
    p_trans.add_argument("--chapters", "-c", help="指定章节ID，逗号分隔 (如 ch01,ch02)")
    p_trans.add_argument("--force", "-f", action="store_true", help="强制重翻已完成的块")
    p_trans.add_argument("--glossary-mode", choices=["auto", "manual"], default="auto", help="术语库模式 (auto: 自动采纳, manual: 需人工审核)")
    p_trans.add_argument("--no-polish", action="store_true", help="禁用润色")
    p_trans.add_argument("--temp-draft", type=float, default=0.1, help="直译温度")
    p_trans.add_argument("--temp-polish", type=float, default=0.7, help="润色温度")
    p_trans.set_defaults(func=cmd_translate)
    
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        return
    
    args.func(args)

if __name__ == "__main__":
    if sys.platform.startswith('win'):
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    main()
