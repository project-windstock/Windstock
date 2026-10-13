"""Generate the R8 obfuscation dictionary: random identifiers, one per line.

Randomised rather than sequential so the renamed symbols carry no ordering information, and long
enough (7-11 chars) that a reader cannot hold them in their head across a decompiled listing.
Java keywords and identifiers that a decompiler would render with special meaning are excluded.
"""
import random
import string

random.seed()  # fresh names every build; pass a seed to make a build reproducible
KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class", "const",
    "continue", "default", "do", "double", "else", "enum", "extends", "final", "finally", "float",
    "for", "goto", "if", "implements", "import", "instanceof", "int", "interface", "long",
    "native", "new", "package", "private", "protected", "public", "return", "short", "static",
    "strictfp", "super", "switch", "synchronized", "this", "throw", "throws", "transient", "try",
    "void", "volatile", "while", "true", "false", "null",
}

words = set()
while len(words) < 4096:
    head = random.choice(string.ascii_lowercase)
    body = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(random.randint(6, 10)))
    word = head + body
    if word not in KEYWORDS and not word.startswith("Java_") and word not in words:
        words.add(word)

with open("project2/launcher/app/obfuscation-dictionary.txt", "w", newline="\n") as out:
    out.write("\n".join(sorted(words)) + "\n")
print(f"{len(words)} identifiers")
