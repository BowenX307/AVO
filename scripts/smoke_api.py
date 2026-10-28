"""Send one plain text request to muse-spark-1.3 to check the API key works."""

from avo.agent import MODEL, make_client


def main() -> None:
    client = make_client()
    response = client.responses.create(
        model=MODEL,
        input="Reply with exactly: AVO smoke test OK",
    )
    print(f"model: {response.model}")
    print(f"reply: {response.output_text}")
    if response.usage:
        print(f"usage: {response.usage.input_tokens} in / {response.usage.output_tokens} out")


if __name__ == "__main__":
    main()
