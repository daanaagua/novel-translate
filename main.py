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
from src.core.exporter import BookExporter
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
        global_config = config_loader.load_config()
        chunking = global_config.get("translation", {}).get("chunking", {})
        project = project_manager.create_project(
            book_id=args.book_id,
            source_path=args.file,
            force=args.force,
            max_chunk_tokens=int(chunking.get("max_tokens", 1100)),
            overlap_sentences=int(chunking.get("overlap_sentences", 0)),
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
    translation_settings = global_config.get("translation", {})
    draft_settings = translation_settings.get("draft", {})
    polish_settings = translation_settings.get("polish", {})
    memory_settings = translation_settings.get("memory", {})
    trans_config = TranslationConfig(
        enable_polish=not args.no_polish,
        draft_temperature=(args.temp_draft if args.temp_draft is not None else float(draft_settings.get("temperature", 0.1))),
        draft_max_tokens=int(draft_settings.get("max_tokens", 6144)),
        polish_temperature=(args.temp_polish if args.temp_polish is not None else float(polish_settings.get("temperature", 0.2))),
        polish_max_tokens=int(polish_settings.get("max_tokens", 6144)),
        glossary_mode=args.glossary_mode,
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
    total_failed = 0
    total_review = 0
    stop_requested = False
    
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
            if args.max_chunks is not None and total_processed >= args.max_chunks:
                stop_requested = True
                break
            # 检查是否需要跳过
            if project.memory.should_skip(chunk, force=args.force):
                print(f"  [SKIP] Chunk {chunk.id}: 已完成，跳过")
                total_skipped += 1
                continue
            
            print(f"  [TRANS] 翻译 Chunk {chunk.id} (Tokens: {chunk.token_count})...")
            
            # 获取上下文
            context = project.memory.get_context_for_chunk(
                chunk,
                summary_max_chars=int(memory_settings.get("rolling_summary_max_chars", 1200)),
                recent_chunks=int(memory_settings.get("recent_chunks", 2)),
                recent_source_chars=int(memory_settings.get("recent_source_chars", 600)),
                recent_translation_chars=int(memory_settings.get("recent_translation_chars", 1000)),
            )
            
            # 执行翻译 (带流式回调)
            result_chunk = engine.translate_chunk(
                chunk, 
                memory_context=context,
                stream_callback=None if args.quiet else stream_printer,
            )
            
            print("") # 换行
            
            # 保存结果
            project.memory.save_chunk(result_chunk)
            project.memory.update_long_term_memory(
                result_chunk,
                recent_chunks=int(memory_settings.get("recent_chunks", 2)),
                summary_max_chars=int(memory_settings.get("rolling_summary_max_chars", 1200)),
                recent_source_chars=int(memory_settings.get("recent_source_chars", 600)),
                recent_translation_chars=int(memory_settings.get("recent_translation_chars", 1000)),
            )
            
            if result_chunk.status == ChunkStatus.COMPLETED:
                print(f"     [OK] 完成")
            elif result_chunk.status == ChunkStatus.HUMAN_REVIEW:
                total_review += 1
                print(f"     [WARN] 润色不完整，已用完整初稿兜底并标记复核")
            else:
                print(f"     [ERR] 失败: {result_chunk.error_message}")
                total_failed += 1
            
            total_processed += 1

        if stop_requested:
            break
            
    print("\n" + "="*40)
    print(f"[DONE] 任务结束")
    print(f"处理: {total_processed}")
    print(f"跳过: {total_skipped}")
    print(f"失败: {total_failed}")
    print(f"待复核: {total_review}")
    return 1 if total_failed else 0

def cmd_export(args):
    """导出完整译文为TXT和EPUB。"""
    try:
        project = project_manager.load_project(args.book_id)
        result = BookExporter(project).export(
            output_dir=args.output_dir,
            require_complete=not args.allow_incomplete,
        )
    except Exception as exc:
        print(f"[ERROR] 导出失败: {exc}")
        return 1
    print(f"[OK] TXT: {result.txt_path}")
    print(f"[OK] EPUB: {result.epub_path}")
    print(f"章节: {result.chapter_count}，文本块: {result.chunk_count}")
    return 0

def main():
    parser = argparse.ArgumentParser(
        description="DeepNovel-Translator CLI (v3.0)",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")
    
    # init
    p_init = subparsers.add_parser("init", help="初始化新项目")
    p_init.add_argument("book_id", help="项目ID (英文，无空格)")
    p_init.add_argument("file", help="小说原文路径 (.txt/.md/.docx/.epub)")
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
    p_trans.add_argument("--glossary-mode", choices=["auto", "manual"], default="manual", help="术语库模式 (auto: 自动采纳, manual: 需人工审核)")
    p_trans.add_argument("--no-polish", action="store_true", help="禁用润色")
    p_trans.add_argument("--temp-draft", type=float, default=None, help="覆盖配置中的第一层温度")
    p_trans.add_argument("--temp-polish", type=float, default=None, help="覆盖配置中的第二层温度")
    p_trans.add_argument("--max-chunks", type=int, default=None, help="本次最多翻译多少个新文本块，适合试跑")
    p_trans.add_argument("--quiet", action="store_true", help="不实时打印模型思考和译文，只显示进度与结果")
    p_trans.set_defaults(func=cmd_translate)

    # export
    p_export = subparsers.add_parser("export", help="导出TXT和EPUB阅读版")
    p_export.add_argument("book_id", help="项目ID")
    p_export.add_argument("--output-dir", help="导出目录，默认 projects/<book_id>/exports")
    p_export.add_argument("--allow-incomplete", action="store_true", help="允许导出尚未翻译完整的项目")
    p_export.set_defaults(func=cmd_export)
    
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        return
    
    result = args.func(args)
    return result if isinstance(result, int) else 0

if __name__ == "__main__":
    if sys.platform.startswith('win'):
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    raise SystemExit(main())
