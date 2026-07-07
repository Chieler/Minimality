from pydantic import BaseModel
from spacy.matcher import PhraseMatcher
import instructor
import spacy
class Entity(BaseModel):
    id: str
    name: str
    aliases: list[str] = []
    type: str
    version: str | None = None
    description: str
    confidence: float


class Relationship(BaseModel):
    source_entity: str
    target_entity: str
    relationship_type: str
    evidence: str
    confidence: float


class ExtractionResult(BaseModel):
    entities: list[Entity]
    relationships: list[Relationship]


class Entity_Extractor():
    def __init__(self):
        self.client = instructor.from_provider("ollama/qwen3.5:9b-mlx")
        self.nlp = spacy.load("en_core_web_sm")
        self.matcher = PhraseMatcher(self.nlp.vocab)
    def extract_new_ExtractionResult(self, text: str):
        Prompt = """
                Extract technical entities and relationships from the text.

                Rules:
                - Use ONLY exact text spans for evidence
                - Do not hallucinate entities not in text
                - Normalize entities (no duplicates)

                Entity types:
                Programming Language, Library, Framework, Tool, Runtime, Database, API,
                SDK, Package Manager, Build Tool, OS, Cloud Platform, IDE, VCS, Testing Framework, ML Framework

                Return structured output following the schema exactly.
                """

        relationship = self.client.create(response_model=ExtractionResult, messages=[{"role" : "user", "content": Prompt + "\n\n" + text}])
        return relationship
    

        