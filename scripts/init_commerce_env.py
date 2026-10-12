"""Generate deployment secrets without printing them or replacing an existing file."""
import argparse
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".env.prod")
    args = parser.parse_args()
    path = Path(args.output)
    content = (f"COMMERCE_SIGNING_KEY={secrets.token_hex(32)}\n"
        f"COMMERCE_MYSQL_PASSWORD={secrets.token_hex(20)}\n"
        f"COMMERCE_MYSQL_ROOT_PASSWORD={secrets.token_hex(24)}\n"
        "COMMERCE_HTTP_PORT=8080\nCOMMERCE_DEMO_ENABLED=true\nCOMMERCE_LLM_ENABLED=false\nDASHSCOPE_API_KEY=\n")
    with path.open("x", encoding="utf-8") as file:
        file.write(content)
    print(f"Created {path}. Existing files are never overwritten.")


if __name__ == "__main__":
    main()
