from pathlib import Path
from src.core.preprocessor import TextPreprocessor

prep = TextPreprocessor()
text = prep.load_text("Book of the New Sun.docx")

print(f"Total length: {len(text)}")
lines = text.split('\n')
print(f"Total lines: {len(lines)}")

# 打印前 500 行，看看结构
for i, line in enumerate(lines[:500]):
    print(f"{i:04d}: {repr(line)}")
