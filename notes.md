## What Jev is best at
used for classifying, detecting, sorting, routing, verifying
Gives probabilities, scores, or yes/no answers to things very fast


state + typed questions --> Jev --> probability distributions /typed decisions --> ordinary application code

Jev should supply narrow semantic judgements where ordinary deterministic logic is insufficient.


## What a Jev Request Looks like
A Jev request must contain a state plus one question
A state can be anything like string, JSON, report, a message
State is everything you would place in front of a panel of experts before asking them to make a judgement.

## Jev's primitive types
1. Noul: Use when you have a basic T/F question	--> Can be used for binary classification
2. Choice:	Which of these options?	One category/option should win
3. Score:	Where does this sit on a scale?	Like how severe is this situation or how mad does this customer sound are some use cases

For Choice and Score, Jev returns an entire probability distribution.
TypeSafe trains System One models using an approach it calls: RLCD — Reinforcement Learning for Calibrated Decisions.
for choice: Jev returns: the winning choice; a probability for every option; a confidence value describing how strongly the distribution favors a particular answer.

One of Jev's most important characteristics is that uncertainty is part of the normal API response.
For Choice and Score, Jev returns an entire probability distribution.

A major architectural difference from autoregressive LLMs is that Jev evaluates independent questions in parallel.
LLMs predict the next token but Jev predicts the next distribution, very very powerful
Questions within one Jev request are evaluated independently against the same state.









Sources:
[source 1](https://gist.github.com/pjburnhill/adf8d28efcad9df037bfdece178ef965)

