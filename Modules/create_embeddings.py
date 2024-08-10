from Modules.logger_setup import setup_logger
import json
from typing import List, Tuple
from pydantic import BaseModel, field_validator
import warnings

# Set up logger
logger = setup_logger()

# Suppress specific warnings from transformers library
warnings.filterwarnings(
    "ignore",
    message="A parameter name that contains `beta` will be renamed internally to `bias`.",
)
warnings.filterwarnings(
    "ignore",
    message="A parameter name that contains `gamma` will be renamed internally to `weight`.",
)

import nltk
import torch
import pandas as pd
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from hdbscan import HDBSCAN
from gensim import corpora, models
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize
from collections import Counter, defaultdict
from textblob import TextBlob
from transformers import BertTokenizer, BertModel
import os

# Initialize BERT model and tokenizer
tokenizer = BertTokenizer.from_pretrained("bert-base-uncased")
model = BertModel.from_pretrained("bert-base-uncased")

# Download NLTK resources if needed
nltk.download("punkt")
nltk.download("stopwords")

# Additional stopwords for category refinement
additional_stopwords = {
    "get",
    "great",
    "like",
    "really",
    "good",
    "gym",
    "place",
    "love",
    "hate",
    "one",
    "trainer",
}


class ReviewInput(BaseModel):
    reviews: List[str]

    @field_validator("reviews")
    def check_non_empty(cls, v):
        if not v:
            raise ValueError("Review cannot be empty")
        return v


def _preprocess_text(texts):
    logger.info("Preprocessing text")
    stop_words = set(stopwords.words("english"))
    preprocessed_texts = [
        [
            word
            for word in word_tokenize(document.lower())
            if word.isalpha() and word not in stop_words
        ]
        for document in texts
    ]
    logger.info("Text preprocessing completed")
    return preprocessed_texts


def _get_bert_embeddings(sentences):
    logger.info("Getting BERT embeddings")
    inputs = tokenizer(
        sentences, return_tensors="pt", padding=True, truncation=True, max_length=512
    )
    with torch.no_grad():
        outputs = model(**inputs)
    embeddings = outputs.last_hidden_state.mean(dim=1).numpy()
    logger.info("BERT embeddings obtained")
    return embeddings


def _calculate_center(df):
    logger.info("Calculating cluster centers")
    centers = (
        df.groupby("Cluster")["bert_embeddings"]
        .apply(lambda x: np.mean(np.vstack(x), axis=0))
        .to_dict()
    )
    logger.info("Cluster centers calculated")
    return centers


def _find_closest_sentence(df, centers):
    logger.info("Finding closest sentences to cluster centers")
    closest_sentences = {}
    for cluster, center in centers.items():
        cluster_embeddings = np.vstack(df[df["Cluster"] == cluster]["Vector"].values)
        distances = np.linalg.norm(cluster_embeddings - center, axis=1)
        closest_index = np.argmin(distances)
        closest_sentences[cluster] = df[df["Cluster"] == cluster]["Sentences"].values[
            closest_index
        ]
    logger.info("Closest sentences found")
    return closest_sentences


def _get_combined_categories(ldamodel, num_topics, num_keywords=5):
    logger.info("Getting combined categories from LDA model")
    all_keywords = []
    for i in range(num_topics):
        topic_terms = ldamodel.show_topic(i)
        all_keywords.extend([word for word, _ in topic_terms])
    keyword_counts = Counter(all_keywords)
    filtered_keywords = {
        word: count
        for word, count in keyword_counts.items()
        if word not in additional_stopwords
    }
    most_common_keywords = [
        word for word, count in Counter(filtered_keywords).most_common(num_keywords)
    ]
    logger.info("Combined categories obtained")
    return most_common_keywords


def analyze_reviews(reviews: List[str]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Analyze a list of reviews to extract topics, sentiments, and polarities.

    Args:
        reviews (List[str]): A list of review strings.

    Returns:
        Tuple[pd.DataFrame, pd.DataFrame]: A tuple containing two DataFrames:
            - The first DataFrame contains the processed reviews with assigned labels and sentiments.
            - The second DataFrame contains summaries of average sentiments and polarities for each category.
    """
    # Validate input
    ReviewInput(reviews=reviews)

    logger.info("Starting review analysis")
    preprocessed_reviews = _preprocess_text(reviews)
    dictionary = corpora.Dictionary(preprocessed_reviews)
    corpus = [dictionary.doc2bow(text) for text in preprocessed_reviews]
    num_topics = 5
    lda_model = models.LdaModel(
        corpus=corpus,
        id2word=dictionary,
        num_topics=num_topics,
        random_state=42,
        passes=15,
        iterations=100,
    )
    labels = _get_combined_categories(lda_model, num_topics)
    processed_reviews = [" ".join(text) for text in preprocessed_reviews]
    df = pd.DataFrame(reviews, columns=["Sentences"])
    df["processed_sentences"] = processed_reviews
    label_texts = [" ".join([label, "review is"]) for label in labels]
    embeddings = _get_bert_embeddings(processed_reviews)
    label_embeddings = _get_bert_embeddings(label_texts)
    df["bert_embeddings"] = embeddings.tolist()
    clusterer = HDBSCAN(
        min_cluster_size=2,
        min_samples=2,
        metric="euclidean",
        cluster_selection_method="leaf",
        prediction_data=True,
    )
    clusterer.fit(embeddings)
    df["Cluster"] = clusterer.labels_
    centers = _calculate_center(df)
    threshold = 0.62
    assigned_labels = {}
    for cluster_id, centroid in centers.items():
        similarities = cosine_similarity([centroid], label_embeddings)[0]
        assigned_labels[cluster_id] = [
            i for i, sim in enumerate(similarities) if sim > threshold
        ]
        if not assigned_labels[cluster_id]:
            assigned_labels[cluster_id] = [np.argmax(similarities)]

    # Debugging: Log assigned labels and label tracker dictionary
    logger.info(f"Assigned labels: {assigned_labels}")

    logger.info(f"Label tracker dictionary: {labels}")

    df["assigned_label"] = df["Cluster"].map(assigned_labels)

    # Debugging: Log assigned labels in DataFrame
    logger.info(f"Assigned labels in DataFrame: {df['assigned_label']}")

    # Create a dictionary to map indices to labels
    label_dict = {i: label for i, label in enumerate(labels)}

    # Apply the mapping with a check for iterable values
    df["named_labels"] = df["assigned_label"].apply(
        lambda x: [label_dict[num] for num in x] if isinstance(x, list) else []
    )

    # Debugging: Log named labels in DataFrame
    logger.info(f"Named labels in DataFrame: {df['named_labels']}")

    # Check for NaN values in 'assigned_label' and log them
    nan_assigned_labels = df[df["assigned_label"].isna()]
    if not nan_assigned_labels.empty:
        logger.warning(f"NaN values found in 'assigned_label': {nan_assigned_labels}")

    sentiments = [TextBlob(review).sentiment[0] * 2.5 + 2.5 for review in reviews]
    polarities = [TextBlob(review).sentiment[1] * 2.5 + 2.5 for review in reviews]
    df["sentiment"] = sentiments
    df["polarity"] = polarities
    categories_summaries_sentiments = defaultdict(list)
    categories_summaries_polarities = defaultdict(list)
    for i, row in df.iterrows():
        for label in row["named_labels"]:
            categories_summaries_sentiments[label].append(row["sentiment"])
            categories_summaries_polarities[label].append(row["polarity"])
    df_summaries = pd.DataFrame(
        {
            "Category": categories_summaries_sentiments.keys(),
            "Average Sentiment": [
                np.mean(vals) for vals in categories_summaries_sentiments.values()
            ],
            "Average Polarity": [
                np.mean(vals) for vals in categories_summaries_polarities.values()
            ],
        }
    )

    # Drop the 'bert_embeddings', 'processed_sentences', and 'Sentences' columns before returning the DataFrame
    df.drop(
        columns=["bert_embeddings", "processed_sentences", "Sentences"], inplace=True
    )

    logger.info("Review analysis completed")
    return df, df_summaries


if __name__ == "__main__":
    # Example usage
    reviews = [
        "The staff at this gym are incredibly friendly and helpful. They always go the extra mile to make sure I have a great workout experience.",
        "The equipment is top-notch and well-maintained. They have a wide variety of machines for all my training needs.",
        "The gym is always clean and well-organized. It's a pleasure to work out in such a pleasant environment.",
        "The staff could be a bit more attentive, but the equipment is good overall.",
        "This gym is a bit dirty at times, but the staff is friendly and the classes are great.",
    ]
    df, df_summaries = analyze_reviews(reviews)

    print(df_summaries)
    print(df)
