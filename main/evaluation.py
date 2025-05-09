import os
import json
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
import re
from utils.utils import init_client
import matplotlib.pyplot as plt
import concurrent.futures
import tqdm
from collections import defaultdict
from tenacity import retry, stop_after_attempt,wait_exponential
import random
import pandas as pd
from matplotlib import font_manager

class Evaluation:
    def __init__(self, base_path, turn=2, config=None):
        """
        Initialize the evaluator
        
        Args:
            base_path: Base path for evaluation
            turn: Turn number (default 2)
            config: Configuration dictionary, may contain:
                - embedding_model_path: Path to embedding model
                - evaluation_root: Root directory for evaluation
                - openai_config: OpenAI configuration
        """
        self.base_path = base_path
        self.turn = turn
        
        # Load config or use defaults
        self.config = config or {}
        self.embedding_model_path = self.config.get(
            "embedding_model_path", 
            "/home/shuaichen/all-MiniLM-L6-v2"
        )
        self.evaluation_root = self.config.get(
            "evaluation_root",
            "/home/shuaichen/code/virtual_scientists/evaluation"
        )
        
        self.client = init_client()
        
        # Initialize directories
        self.evaluation_dir = None
        self.abstract_dir = None 
        self.ranking_score_dir = None
        self.image_dir = None
        
        # Cache
        self._embedding_model = None
        self._embedding_cache = {}
    
    def get_embedding(self, sentences_list):
        """Get text embeddings with caching
        
        Args:
            sentences_list: List of sentences to embed
            
        Returns:
            numpy.ndarray: Array of embeddings, None if failed
        """
        if not sentences_list:
            return None
            
        try:
            # Check cache
            cached = []
            to_compute = []
            for sentence in sentences_list:
                if sentence in self._embedding_cache:
                    cached.append(self._embedding_cache[sentence])
                else:
                    to_compute.append(sentence)
            
            # Only compute uncached
            if to_compute:
                if self._embedding_model is None:
                    self._embedding_model = SentenceTransformer(self.embedding_model_path)
                
                new_embeddings = self._embedding_model.encode(to_compute)
                for sentence, embedding in zip(to_compute, new_embeddings):
                    self._embedding_cache[sentence] = embedding
                    cached.append(embedding)
            
            return np.array(cached)
        except Exception as e:
            print(f"Error generating embeddings: {str(e)}", file=sys.stderr)
            return None

    def deepseek_chat(self, sys_prompt, prompt):
        """Call DeepSeek API for chat completion"""
        try:
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ],
                stream=False
            )
            return response.choices[0].message.content, response.usage.total_tokens
        except Exception as e:
            print(f"API call error: {str(e)}")
        return None, 0
    
    def get_top_ideas(self, base_path):
        """Get paths for all top ideas"""
        top_idea_paths = []
        for idea_id in range(len(os.listdir(base_path))):
            top_ideas_dir = os.path.join(base_path, f"idea_{idea_id}", f"turn_{self.turn}", "ranking")
            if not os.path.exists(top_ideas_dir):
                continue
            else:
                for seed_folder in os.listdir(top_ideas_dir):
                    seed_path = os.path.join(top_ideas_dir, seed_folder, "top_ideas.json")
                    try:
                        with open(seed_path, "r") as f:
                            top_ideas = json.load(f)
                            top_idea_paths.extend(list(top_ideas.keys()))
                    except Exception as e:
                        print(f"Error reading file {seed_path}: {str(e)}")
        return top_idea_paths

    def generate_abstract(self, top_idea_path, abstract_dir, id):
        """Generate abstract for a single idea"""
        # Get original filename and create corresponding abstract file path
        abstract_filename = f"abstract_{id}.json"
        # Join abstract_dir and abstract_filename
        abstract_path = os.path.join(abstract_dir, abstract_filename)
        # If abstract file exists, read and return directly
        if os.path.exists(abstract_path):
            try:
                with open(abstract_path, 'r') as f:
                    abstract_content = json.load(f)
                    title_abstract = abstract_content['Title'] + "\n" + abstract_content['Abstract']
                    return title_abstract
            except Exception as e:
                    print(f"Error reading existing abstract {abstract_path}: {str(e)}")

        try:
            with open(top_idea_path, 'r') as file:
                top_idea = json.load(file)
                title = top_idea['Title']
                idea = top_idea['Idea']
                experiment = top_idea['Experiment']

                sys_prompt = """You are an ambitious scientist who will generate a summary based on given research idea and experimental steps."""

                prompt = f"""
                        Requirements: The content of the abstract should cover: research questions and objectives, research methods, expected research results, and conclusions.Do not exceed 300 words.
                        Here is the research idea: '''{title}\nIdea: {idea}'''
                        Here is the experimental steps: '''{experiment}'''
                        Please respond in the following format: 
                        Thought: <THOUGHT> 
                        Abstract: ```json<JSON>```
                        In <THOUGHT>, please briefly describe your thinking.
                        In <JSON>, provide the abstract with the following fields: 
                        - "Title": A title for the abstract.
                        - "Abstract": abstract.
                        Be cautious and realistic on your ratings.
                        """

                response, _ = self.deepseek_chat(sys_prompt, prompt)
                if response:
                    json_blocks = re.findall(r'```json(.*?)```', response, re.DOTALL)[0]
                    json_data = json.loads(json_blocks)
                    # Create JSON content
                    abstract_content = {
                        "Title": json_data['Title'],
                        "Abstract": json_data['Abstract'],
                        "Source": top_idea_path
                    }

                    # Save generated abstract as JSON
                    try:
                        with open(abstract_path, 'w') as f:
                            json.dump(abstract_content, f, indent=4)
                    except Exception as e:
                        print(f"Error saving abstract {abstract_path}: {str(e)}")

                    # Return title and abstract combined
                    return f"{json_data['Title']}\n{json_data['Abstract']}"
        except Exception as e:
            print(f"Error generating abstract {top_idea_path}: {str(e)}")
        return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10))
    def better_idea(self,abstract_1, abstract_2, method):
        sys_prompt = "You are a reviewer specialized in Natural Language Processing. You are given two abstracts. One of them is accepted by a top AI conference (like ICLR or ACL) and the other one is rejected. Your task is to identify the one that has been accepted.\n"
        prompt = ""
        if "zero_shot" in method:
            prompt += "The two project proposals are:\n\n" 
            prompt += "abstract 1:\n" + abstract_1+ "\n\n"
            prompt += "abstract 2:\n" + abstract_2 + "\n\n"
            # prompt += "\nYou can consider factors like novelty, soundness, excitement, and potential impact.\n"
            if method == "zero_shot":
                prompt += "Now decide which one is the accepted idea. Directly return a number 1 or 2 and nothing else.\n"
            elif method == "zero_shot_cot":
                prompt += "Now decide which one is the accepted idea. Think step by step by writing a meta-review to compare the strengths and weaknesses of both ideas and explain why one idea is better than the other. After the meta-review, start a new line and directly return a number 1 or 2 to indicate the accepted idea and end the response.\n"

        response, cost = self.deepseek_chat(sys_prompt, prompt) 
        return response, cost


    def tournament_ranking(self, abstract_lst, filename_lst, ranking_score_dir, max_round=5):
        scores = defaultdict(lambda: 1)
        all_costs = 0
        os.makedirs(ranking_score_dir, exist_ok=True)
        if len(os.listdir(ranking_score_dir)) > 0:
            return None, None, None, True
        
        def single_round(abstract_lst, current_round=0, all_costs=0):
            if current_round == 0:
                random.shuffle(abstract_lst)

            match_pairs = []
            sorted_abstracts = sorted(abstract_lst, key=lambda abstract: scores[abstract[:200]], reverse=True)
            for i in range(0, len(sorted_abstracts), 2):
                if i + 1 < len(sorted_abstracts):
                    match_pairs.append((sorted_abstracts[i], sorted_abstracts[i+1]))
                else:
                    # If there is an odd number of ideas, the last one automatically wins this round
                    scores[sorted_abstracts[i][:200]] += 1

            for abstract1, abstract2 in tqdm.tqdm(match_pairs):
                result, cost = self.better_idea(abstract1, abstract2, "zero_shot")
                if result.strip() == '1':
                    scores[abstract1[:200]] += 1
                else:
                    scores[abstract2[:200]] += 1
                all_costs += cost
            return all_costs
        
        # Conduct the tournament rounds until only one idea remains
        current_round = 0
        score_predictions = {}
        while current_round < max_round:
            print("Current round: ", current_round + 1)
            all_costs = single_round(abstract_lst[:], current_round=current_round, all_costs=all_costs)
            current_round += 1

            # Convert scores to a list matching the order of the original idea list
            final_scores = [scores[abstract[:200]] for abstract in abstract_lst]
            for i in range(len(filename_lst)):
                score_predictions[filename_lst[i]] = final_scores[i]
            os.makedirs(ranking_score_dir, exist_ok=True)
            # Save all scores
            cache_file = os.path.join(ranking_score_dir, "round_{}.json".format(current_round))
            if not os.path.exists(os.path.dirname(cache_file)):
                os.makedirs(os.path.dirname(cache_file))
            with open(cache_file, "w") as f:
                json.dump(score_predictions, f, indent=4)
                        
        top_ideas = {}
        sorted_ideas_with_idx = [(idx, (fname, score)) for idx, (fname, score) 
                               in enumerate(zip(filename_lst, final_scores))]
        
        sorted_ideas_with_idx = sorted(sorted_ideas_with_idx, 
                                     key=lambda x: x[1][1], 
                                     reverse=True)
        for idx, (idea_name, score) in sorted_ideas_with_idx:
            top_ideas[idea_name] = {
                "idea": abstract_lst[idx],
                "ai_ranking_score": score,
            }
        
        abstract_ranking_file = os.path.join(ranking_score_dir, "abstract_ranking_score.json")
        with open(abstract_ranking_file, "w") as f:
            json.dump(top_ideas, f, indent=4)
        
        return top_ideas, final_scores, all_costs, False

    def setup_directories(self, paper_id):
        """Set up directory structure for evaluation
        
        Args:
            paper_id: Paper ID
        """
        self.evaluation_dir = os.path.join(
            self.evaluation_root,
            str(paper_id),
            f"turn_{self.turn}"
        )
        self.abstract_dir = os.path.join(self.evaluation_dir, "abstract")
        self.ranking_score_dir = os.path.join(self.evaluation_dir, "ranking_score")
        self.image_dir = os.path.join(self.evaluation_dir, "image")
        
        try:
            for directory in [self.evaluation_dir, self.abstract_dir, 
                             self.image_dir, self.ranking_score_dir]:
                os.makedirs(directory, exist_ok=True)
        except OSError as e:
            print(f"Failed to create directory: {str(e)}", file=sys.stderr)
            raise
    
    def process_abstracts(self, top_idea_paths: list) -> list:
        """Process and generate abstracts for all ideas in parallel
        
        Args:
            top_idea_paths: List of idea file paths
            
        Returns:
            list: List of successfully generated abstracts, None for failures
        """
        if not top_idea_paths:
            return []
            
        abstract_list = [None] * len(top_idea_paths)
        completed = 0
        total = len(top_idea_paths)
        
        max_workers = min(10, len(top_idea_paths))
        
        with tqdm.tqdm(total=total, desc="Generating abstracts") as pbar:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_id = {
                    executor.submit(
                        self.generate_abstract, 
                        path, 
                        self.abstract_dir, 
                        id
                    ): id 
                    for id, path in enumerate(top_idea_paths)
                }
                
                for future in concurrent.futures.as_completed(future_to_id):
                    id = future_to_id[future]
                    try:
                        abstract_list[id] = future.result()
                        completed += 1
                        pbar.update(1)
                    except Exception as e:
                        print(f"Error processing ID {id}: {str(e)}", file=sys.stderr)
                    finally:
                        pbar.set_postfix({
                            'Completed': f"{completed}/{total}",
                            'Success rate': f"{(completed/total)*100:.1f}%"
                        })
        
        return [a for a in abstract_list if a is not None]
    
    def get_abstract_contents(self) -> list:
        """Get contents of all abstracts
        
        Returns:
            list: List of abstract contents in "Title\nAbstract" format
            
        Raises:
            FileNotFoundError: If abstract directory doesn't exist
        """
        if not os.path.exists(self.abstract_dir):
            raise FileNotFoundError(f"Abstract directory not found: {self.abstract_dir}")
            
        abstract_content_list = []
        for filename in os.listdir(self.abstract_dir):
            if not filename.endswith('.json'):
                continue
                
            abstract_path = os.path.join(self.abstract_dir, filename)
            try:
                abstract_content = self._read_json_file(abstract_path)
                abstract_content_list.append(
                    f"{abstract_content['Title']}\n{abstract_content['Abstract']}"
                )
            except Exception as e:
                print(f"Error reading abstract file {filename}: {str(e)}", file=sys.stderr)
                continue
                
        return abstract_content_list
    
    def get_reference_papers(self, top_idea_paths: list) -> list:
        """Get reference papers for each idea
        
        Args:
            top_idea_paths: List of idea file paths
            
        Returns:
            list: List of reference papers per idea (each as "title+abstract" string)
            
        Raises:
            ValueError: If input path list is empty
        """
        if not top_idea_paths:
            raise ValueError("Input idea path list cannot be empty")
            
        ref_papers_list = []
        for idea_path in top_idea_paths:
            try:
                idea_content = self._read_json_file(idea_path)
                ref_papers = [
                    f"{ref['title']}{ref['abstract']}" 
                    for ref in idea_content.get('ref_papers', [])[:10]
                ]
                ref_papers_list.append(ref_papers)
            except Exception as e:
                print(f"Error processing references {idea_path}: {str(e)}", file=sys.stderr)
                ref_papers_list.append([])
                
        return ref_papers_list
    
    def _calculate_metrics(self, abstract_list: list, ref_papers_list: list) -> dict:
        """Calculate evaluation metrics
        
        Args:
            abstract_list: List of abstracts
            ref_papers_list: List of reference papers
            
        Returns:
            dict: Dictionary containing evaluation metrics
        """
        # Calculate ranking scores
        scores_ratio = self._calculate_scores_ratio()
        
        # Calculate reference paper similarity metrics
        ref_similarity = calculate_ref_abstract_similarity(abstract_list, ref_papers_list)
        
        # Calculate inter-idea similarity metrics
        embeddings = self.get_embedding(abstract_list)
        metrics = calculate_similarity_metrics(embeddings)
        
        return {
            'idea_num': len(abstract_list),
            'similarity_ratio': metrics['ratio'],
            'unique_ratio': metrics['unique_ratio'],
            'novelty_ratio': ref_similarity['novel_ideas_ratio'],
            'high_score_ratio': scores_ratio
        }

    def evaluate_ideas(self, paper_id: int) -> dict:
        """Evaluate ideas for a single paper ID
        
        Args:
            paper_id: Paper ID to evaluate
            
        Returns:
            dict: Dictionary containing evaluation results
        """
        try:
            self.setup_directories(paper_id)
            
            # Get idea paths
            if paper_id in [1001, 1002, 1003, 1004, 1005]:
                top_idea_paths = [
                    os.path.join(self.base_path, path) 
                    for path in os.listdir(self.base_path)
                ]
            else:
                top_idea_paths = self.get_top_ideas(self.base_path)
                
            # Process abstracts
            abstract_list = self.process_abstracts(top_idea_paths)
            if not abstract_list:
                raise ValueError("Failed to generate any abstracts")
                
            abstract_content_list = self.get_abstract_contents()
            ref_papers_list = self.get_reference_papers(top_idea_paths)
            
            # Calculate ranking scores
            if len(os.listdir(self.ranking_score_dir)) == 0:
                self.tournament_ranking(abstract_content_list, top_idea_paths, self.ranking_score_dir)
            
            # Calculate and return all metrics
            return self._calculate_metrics(abstract_list, ref_papers_list)
            
        except Exception as e:
            print(f"Error evaluating paper {paper_id}: {str(e)}", file=sys.stderr)
            raise
    
    def _calculate_scores_ratio(self):
        """Calculate ratio of high scores"""
        with open(os.path.join(self.ranking_score_dir, "abstract_ranking_score.json"), "r") as f:
            abstract_ranking_score = json.load(f)
            scores = [content['ai_ranking_score'] for content in abstract_ranking_score.values()]
            high_scores = sum(1 for score in scores if score >= 5)
            return high_scores / len(scores) if scores else 0

def calculate_similarity_metrics(embeddings, threshold=0.8):
    """Calculate similarity metrics"""
    similarity_matrix = cosine_similarity(embeddings)
    upper_triangle_mask = np.triu(np.ones_like(similarity_matrix), k=1).astype(bool)
    upper_triangle_values = similarity_matrix[upper_triangle_mask]
    
    similar_pairs = np.sum(upper_triangle_values >= threshold)
    total_pairs = len(upper_triangle_values)
    similarity_ratio = similar_pairs / total_pairs if total_pairs > 0 else 0
    
    # Calculate unique ideas
    # For each idea, check if its similarity with all others is below threshold
    n_ideas = len(embeddings)
    unique_ideas = []
    for i in range(n_ideas):
        # Get similarity between current idea and all others
        similarities = similarity_matrix[i]
        # Exclude self-similarity (always 1.0)
        other_similarities = np.concatenate([similarities[:i], similarities[i+1:]])
        # If all similarities are below threshold, consider it a unique idea
        if np.all(other_similarities < threshold):
            unique_ideas.append(i)
    
    unique_ratio = len(unique_ideas) / n_ideas if n_ideas > 0 else 0
    
    return {
        'ratio': similarity_ratio,
        'unique_ideas': unique_ideas,
        'unique_ratio': unique_ratio
    }

def calculate_ref_abstract_similarity(abstract_list, ref_papers_list, novelty_threshold=0.5):
    """Calculate similarity between abstracts and their references
    
    Args:
        abstract_list: List of abstracts
        ref_papers_list: List of reference papers for each abstract
        novelty_threshold: Threshold for considering novelty
    
    Returns:
        dict: Dictionary containing similarity stats and novelty analysis
    """
    # Initialize SentenceTransformer model
    model = SentenceTransformer('/home/shuaichen/all-MiniLM-L6-v2')
    
    # Store all similarity scores
    all_avg_similarities = []
    all_max_similarities = []
    
    # Store novelty analysis results
    novelty_analysis = []
    
    # Calculate similarity for each abstract and its references
    for abstract, ref_papers in zip(abstract_list, ref_papers_list):
        if not ref_papers:
            continue
            
        abstract_embedding = model.encode([abstract])
        ref_embeddings = model.encode(ref_papers)
        similarities = cosine_similarity(abstract_embedding, ref_embeddings)[0]
        
        # Determine novelty
        is_novel = all(sim < novelty_threshold for sim in similarities)
        max_similarity = np.max(similarities)
        avg_similarity = np.mean(similarities)
        
        novelty_analysis.append({
            'is_novel': is_novel,
            'max_similarity': max_similarity,
            'avg_similarity': avg_similarity
        })
        
        all_avg_similarities.append(avg_similarity)
        all_max_similarities.append(max_similarity)
    
    # Build results dictionary
    results = {
        'mean_avg_similarity': np.mean(all_avg_similarities),
        'mean_max_similarity': np.mean(all_max_similarities),
        'std_avg_similarity': np.std(all_avg_similarities),
        'std_max_similarity': np.std(all_max_similarities),
        'novelty_analysis': novelty_analysis,
        'novel_ideas_ratio': sum(1 for x in novelty_analysis if x['is_novel']) / len(novelty_analysis) if novelty_analysis else 0
    }
    
    return results

def plot_team_metrics(team_metrics, selected_metrics=None):
    """Plot metrics trends across teams"""
    # Add Chinese font path
    font_path = '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf'
    font_prop = font_manager.FontProperties(fname=font_path)
    
    plt.figure(figsize=(15, 10))
    all_metrics = ['unique_ratio', 'novelty_ratio', 'high_score_ratio']
    all_labels = ['Uniqueness ratio', 'Novelty ratio', 'High score ratio']
    
    if selected_metrics:
        metrics = [m for m in all_metrics if m in selected_metrics]
        labels = [l for m, l in zip(all_metrics, all_labels) if m in selected_metrics]
    else:
        metrics = all_metrics
        labels = all_labels
    
    # Increase line width and marker size
    for metric, label in zip(metrics, labels):
        values = [team_metrics[team][metric] for team in sorted(team_metrics.keys()) if team != 1000]
        plt.plot(range(2, len(values) + 2), values, marker='o', label=label, linewidth=2.5, markersize=10)
    
    plt.xticks(range(2, len(values) + 2), fontsize=14)
    plt.yticks(fontsize=14)
    
    # Increase label and title font sizes
    plt.xlabel('Team size', fontproperties=font_prop, fontsize=16)
    plt.ylabel('Metric value', fontproperties=font_prop, fontsize=16)
    plt.title('Team evaluation metrics comparison', fontproperties=font_prop, fontsize=18)
    plt.legend(prop=font_prop, fontsize=14)
    plt.grid(True)
    
    # Increase DPI when saving image for better quality
    plt.savefig('/home/shuaichen/code/virtual_scientists/image/team_metrics_comparison.png', dpi=300, bbox_inches='tight')
    plt.close()

def evaluate_team(team_id: int, paper_ids: list, results_dir: str) -> tuple:
    """Evaluate all papers for a single team
    
    Args:
        team_id: Team ID
        paper_ids: List of paper IDs for this team
        results_dir: Directory to store results
        
    Returns:
        tuple: (team metrics dict, total idea count)
    """
    results = []
    total_ideas = 0
    
    for paper_id in paper_ids:
        base_path = os.path.join(results_dir, str(paper_id))
        evaluation = Evaluation(base_path, turn=0)
        result = evaluation.evaluate_ideas(paper_id)
        results.append(result)
        total_ideas += result['idea_num']
    
    # Calculate averages
    avg_metrics = {
        key: np.mean([r[key] for r in results]) 
        for key in ['idea_num', 'similarity_ratio', 'unique_ratio', 'novelty_ratio', 'high_score_ratio']
    }
    
    return {
        'team_id': team_id,
        'avg_ideas': avg_metrics['idea_num'],
        'total_ideas': avg_metrics['idea_num'] * 5,
        'avg_similarity_ratio': avg_metrics['similarity_ratio'],
        'avg_uniqueness_ratio': avg_metrics['unique_ratio'],
        'avg_novelty_ratio': avg_metrics['novelty_ratio'],
        'high_score_ratio': avg_metrics['high_score_ratio']
    }, total_ideas

def print_team_results(team_metrics: dict) -> None:
    """Print team evaluation results
    
    Args:
        team_metrics: Team metrics dictionary
    """
    print(f"\nEvaluation results for team {team_metrics['team_id']}:")
    print(f"Average ideas: {team_metrics['avg_ideas']:.2f}")
    print(f"Total ideas generated: {team_metrics['total_ideas']}") 
    print(f"Average similarity ratio: {team_metrics['avg_similarity_ratio']:.2f}")
    print(f"Average uniqueness ratio: {team_metrics['avg_uniqueness_ratio']:.2f}")
    print(f"Average novelty ratio: {team_metrics['avg_novelty_ratio']:.2f}")
    print(f"Ratio of ideas with score ≥5: {team_metrics['high_score_ratio']:.2%}")

if __name__ == "__main__":
    # Team configuration
    team2id = {
        8:[1,15,42,44,50],
        7:[2,3,12,18,21],
        6:[11,13,33,36,51],
        5:[4,7,28,29,41],
        4:[5,16,17,22,23],
        3:[25,26,37,45,64],
        2:[30,34,53,54,81],
        1:[47,471,472,89,131],
    }
    
    results_dir = "/home/shuaichen/code/virtual_scientists/results"
    excel_path = '/home/shuaichen/code/virtual_scientists/evaluation/team_metrics_0.xlsx'
    
    # Evaluate all teams
    excel_data = []
    total_ideas = 0
    
    for team_id, paper_ids in team2id.items():
        team_metrics, ideas_count = evaluate_team(team_id, paper_ids, results_dir)
        excel_data.append(team_metrics)
        total_ideas += ideas_count
        print_team_results(team_metrics)
    
    print(f"\nTotal ideas generated: {total_ideas}")
    
    # Save results to Excel
    df = pd.DataFrame(excel_data)
    df.to_excel(excel_path, index=False)
    print(f"\nExcel file saved to: {excel_path}")
