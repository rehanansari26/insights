# Copyright (c) 2022, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import hashlib

import frappe
import tiktoken
from langchain.callbacks.base import BaseCallbackHandler
from langchain_community.vectorstores import Chroma
from langchain_openai import OpenAIEmbeddings
from openai import OpenAI

from insights.decorators import check_role
from insights.insights.doctype.insights_data_source.sources.utils import (
    add_limit_to_sql,
)
from insights.utils import InsightsDataSource, get_data_source_dialect


def SystemMessage(content):
    return {"role": "system", "content": content}


def AIMessage(content):
    return {"role": "assistant", "content": content}


def UserMessage(content):
    return {"role": "user", "content": content}


def FunctionMessage(tool_call_id, name, content):
    return {"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": content}


@frappe.whitelist()
@check_role("Insights User")
def create_new_chat():
    empty_chat = frappe.db.exists("Insights Copilot Chat", {"copilot_bot": "Default"})
    if empty_chat:
        return empty_chat

    new_chat = frappe.new_doc("Insights Copilot Chat")
    new_chat.copilot_bot = "Default"
    new_chat.save()
    return new_chat.name


OPENAI_MODEL = "gpt-3.5-turbo-1106"


def count_token(messages, model=OPENAI_MODEL):
    if not isinstance(messages, list):
        messages = [messages]
    return sum((len(tiktoken.encoding_for_model(model).encode(m)) for m in messages))


class SchemaStore:
    def __init__(self, data_source, verbose=False):
        self.data_source = data_source
        self.collection_name = frappe.scrub(data_source)
        self.vector_store_path = frappe.get_site_path(
            "private",
            "files",
            "vector_stores",
            "schema_store",
        )
        self.verbose = verbose

    @property
    def vector_store(self):
        if not hasattr(self, "_vector_store"):
            self.verbose and print("Loading vector store from disk")
            self._vector_store = Chroma(
                collection_name=self.collection_name,
                persist_directory=self.vector_store_path,
                embedding_function=OpenAIEmbeddings(
                    openai_api_key=frappe.conf.get("openai_api_key"),
                ),
            )

        return self._vector_store

    def ingest_schema(self, reset=False):
        schema = self.get_schema()
        schema = self.skip_ingested_tables(schema)

        if not schema and not reset:
            self.verbose and print("No new data to ingest")
            return

        self.verbose and print(f"Ingesting {len(schema)} rows of data")

        ids = [d["id"] for d in schema]
        texts = [d["text"] for d in schema]
        metadata = [d["metadata"] for d in schema]

        # $0.0004 / 1K tokens
        tokens_consumed = count_token(texts, model="text-embedding-ada-002")
        usage = tokens_consumed / 1000 * 0.0004
        self.verbose and print(f"This will consume {usage} USD")

        self._vector_store = Chroma.from_texts(
            ids=ids,
            texts=texts,
            metadatas=metadata,
            collection_name=self.collection_name,
            persist_directory=self.vector_store_path,
            embedding=OpenAIEmbeddings(
                openai_api_key=frappe.conf.get("openai_api_key"),
            ),
        )
        self._vector_store.persist()

    def get_schema(self):
        """
        Returns a list of dicts with attributes: id, text, metadata.
        - ID: A unique identifier for the Table and its Columns.
        - Text: The Table name and Column names.
        - Metadata: The Table name and the number of rows in the table.
        """

        tables = frappe.get_all(
            "Insights Table",
            filters={
                "data_source": self.data_source,
                "is_query_based": 0,
                "hidden": 0,
            },
            fields=["name", "table", "modified"],
        )

        data = []
        doc = InsightsDataSource.get_doc(self.data_source)
        for table in tables:
            query = f"SELECT * FROM `{table.table}` LIMIT 3"
            try:
                results = doc._db.execute_query(query, return_columns=True)
            except BaseException:
                continue

            id = self.get_id_for_table(table)
            text = self.get_text_for_table(table, results)
            metadata = {}
            data.append({"id": id, "text": text, "metadata": metadata})

        return data

    def get_id_for_table(self, table):
        """Returns a unique hash generated from the table name and modified date."""
        name = table.get("name")
        modified = table.get("modified")
        return hashlib.sha256(f"{name}{modified}".encode("utf-8")).hexdigest()

    def get_text_for_table(self, table, results):
        """
        Returns human-readable text to describe the table and columns along with 5 sample rows.
        Example:
            Table: tabUser
            Data: name, email, creation, modified
                  John Doe, john@doe.com, 2021-01-01, 2021-01-01
        """
        ret = f"Table: {table.get('table')}\nData: "
        cols = [col.label for col in results[0]]
        ret += ", ".join(cols)
        for row in results[1:]:
            ret += "\n      "
            ret += ", ".join(str(col)[:20] for col in row)
        return ret

    def get_metadata_for_table(self, table):
        """Returns the table name and the number of rows in the table."""
        return {"table_name": table["table"], "row_count": table["row_count"]}

    def skip_ingested_tables(self, data):
        """Removes the tables that are already ingested."""
        new_ids = [row["id"] for row in data]
        results = self.vector_store._collection.get(ids=new_ids)
        if not results["ids"]:
            return data
        return [row for row in data if row["id"] not in results["ids"]]

    def find_relevant_tables(self, query, k=5):
        docs = self.vector_store.similarity_search(query, k)
        return "\n\n".join(doc.page_content for doc in docs)


SQL_GEN_INSTRUCTIONS = """
You are a data analysis expert called BI_GPT. Your job is to analyze business data by writing SQL queries. You are expert in writing {dialect} SQL queries. Given a question and schema of the database, you can figure out the best possible SQL query that will help gain insights from the database.

Before answering any user's question, always follow these instructions:
About database schema:
- DO NOT make any assumptions about the tables and columns in the database.
- If you don't have enough information, you **MUST** say that you don't have enough information.

About writing SQL queries:
- The user's database is a {dialect} database. So, you should write {dialect} SQL queries.
- DO NOT write any other type of query. write only SELECT queries.
- 90% of the time, users will expect a SELECT query with GROUP BY clauses.
- ALWAYS add a LIMIT clause to the query. The limit should be {limit}.

About answer format:
- DO NOT explain the generated query.
- Use markdown formatting to format the answer.

STRICTLY follow the each and every instruction above. If you don't, you will be banned from the system.
"""


class SQLCopilot:
    def __init__(
        self,
        data_source,
        history=None,
        verbose=False,
    ):
        self.data_source = data_source
        self.history = history or []
        self.verbose = verbose
        self.max_query_limit = 20
        self._function_messages = []
        self.prepare_schema()
        self.client = OpenAI(
            api_key=frappe.conf.get("openai_api_key"),
        )

    def prepare_schema(self):
        self.schema_store = SchemaStore(
            data_source=self.data_source,
            verbose=self.verbose,
        )
        self.database_schema = self.schema_store.get_schema()
        if not self.database_schema:
            raise frappe.ValidationError(
                "No tables found in the database. Please make sure the data source is synced."
            )

    def ask(self, question, stream=False):
        usage = 0
        max_iterations, iteration = 5, 0
        while True:
            messages = self.prepare_messages(question)
            self.verbose and print(messages)
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=self.get_functions(),
                tool_choice="auto",
                temperature=0.1,
            )
            usage += response.usage.total_tokens
            if not self.is_function_call(response) or iteration >= max_iterations:
                break
            self.handle_function_call(response)
            iteration += 1

        final_response = response.choices[0].message.content
        self.history.append(AIMessage(content=final_response))
        self.verbose and print("Usage:", usage, "Cost:", usage / 1000 * 0.0013)
        return final_response

    def prepare_messages(self, question):
        messages = []
        system_prompt = SQL_GEN_INSTRUCTIONS.format(
            dialect=get_data_source_dialect(self.data_source),
            limit=self.max_query_limit,
        )
        messages.append(SystemMessage(content=system_prompt))
        messages += self.get_history_messages()
        messages.append(UserMessage(content=question))
        if self._function_messages:
            messages += self._function_messages
        return messages

    def get_history_messages(self):
        if not self.history:
            return []

        messages = []
        for message in self.history:
            if message["role"] == "assistant":
                messages.append(AIMessage(content=message["message"]))
            else:
                messages.append(UserMessage(content=message["message"]))
        return messages

    def get_functions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_relevant_tables",
                    "description": "A function useful for finding the tables that are relevant to generate a sql query.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The user's question",
                            },
                        },
                        "required": ["question"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "validate_sql_query",
                    "description": "A function useful for validating a sql query.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "A syntactically correct sql query",
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            # {
            #     "type": "function",
            #     "function": {
            #         "name": "execute_sql_query",
            #         "description": "A function useful for executing a sql query.",
            #         "parameters": {
            #             "type": "object",
            #             "properties": {
            #                 "query": {
            #                     "type": "string",
            #                     "description": "A syntactically correct sql query",
            #                 },
            #             },
            #             "required": ["query"],
            #         },
            #     },
            # },
        ]

    def run_function(self, function_name, arguments):
        def _get_relevant_tables(question):
            tables = self.schema_store.find_relevant_tables(question)
            return "Relevant tables:\n\n" + tables

        def execute_sql_query(query):
            source = InsightsDataSource.get_doc(self.data_source)
            limited_query = add_limit_to_sql(query, limit=self.max_query_limit)
            try:
                results = source._db.execute_query(limited_query)
                if not results:
                    return "The query didn't return any results. Try writing a different query."
                return "Results:\n\n" + str(results)

            except BaseException as e:
                return f"ERROR: {e}. Look for corrections and try again."

        def validate_sql_query(query):
            if "SELECT" not in query.upper():
                return "The query is not a SELECT query. Try writing a SELECT query."

            source = InsightsDataSource.get_doc(self.data_source)
            limited_query = add_limit_to_sql(query, limit=10)
            try:
                source._db.execute_query(limited_query)
                self.learn_query(query)
                return "The query is valid."
            except BaseException as e:
                return f"ERROR: {e}. Look for corrections and try again."

        try:
            if function_name == "get_relevant_tables":
                return _get_relevant_tables(**arguments)
            elif function_name == "validate_sql_query":
                return validate_sql_query(**arguments)
            elif function_name == "execute_sql_query":
                return execute_sql_query(**arguments)
        except BaseException as e:
            return f"ERROR: {e}. Look for corrections and try again."

    def is_function_call(self, response):
        response_message = response.choices[0].message
        tool_calls = response_message.tool_calls
        return bool(tool_calls)

    def handle_function_call(self, response):
        response_message = response.choices[0].message
        tool_calls = response_message.tool_calls
        self._function_messages.append(response_message)
        for tool_call in tool_calls:
            function_name = tool_call.function.name
            function_args = frappe.parse_json(tool_call.function.arguments)
            function_response = self.run_function(function_name, function_args)
            self._function_messages.append(
                FunctionMessage(
                    tool_call_id=tool_call.id,
                    name=function_name,
                    content=function_response,
                )
            )

    def learn_query(self, query):
        # generate a question from the query
        # store the pair of question and query in the vector store
        # use the vector store to find the relevant queries for a question
        pass


class StreamOutputCallback(BaseCallbackHandler):
    def on_llm_new_token(self, token, **kwargs):
        frappe.publish_realtime("llm_stream_output", token, user=frappe.session.user)
