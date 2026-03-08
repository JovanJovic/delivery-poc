# AI Project Onboarding: Delivery Proof of Concept

This document provides a comprehensive overview of the Delivery Proof of Concept project, its architecture, and instructions for setup and development.

## Project Overview

This project is a web application designed as a proof of concept for a delivery management system. It allows for the creation of delivery "runs" from an Excel file, tracking of individual orders, and proof of delivery (POD) through photo uploads and signatures. The application provides a simple interface for both delivery drivers and managers.

## Technologies Used

The project is built with the following technologies:

*   **Backend:**
    *   **Python 3.9:** The core programming language.
    *   **FastAPI:** A modern, fast (high-performance) web framework for building APIs with Python.
    *   **Uvicorn:** An ASGI server for running the FastAPI application.
    *   **Pandas:** A library for data manipulation and analysis, used here for reading Excel files.
*   **Database:**
    *   **Google Cloud Firestore:** A flexible, scalable NoSQL cloud database for storing run and order data.
*   **Storage:**
    *   **Google Cloud Storage (GCS):** A cloud object storage service used for storing proof of delivery files (photos and signatures).
*   **Frontend:**
    *   **HTML, CSS, JavaScript:** The frontend is rendered as simple HTML with inline CSS and JavaScript, served directly from the Python backend.
*   **Deployment:**
    *   The application is configured for deployment on a platform that supports Python runtimes, as indicated by the `app.yaml` file (e.g., Google App Engine).

## Prerequisites

Before you can run this project, you will need the following:

*   **Python 3.9:** Make sure you have Python 3.9 installed on your system.
*   **Google Cloud Project:** A Google Cloud project with the following APIs enabled:
    *   Cloud Firestore API
    *   Cloud Storage API
*   **Service Account Credentials:** You will need to have a Google Cloud service account with permissions to access Firestore and GCS. The application uses Application Default Credentials (ADC), so you should configure them in your local environment.
*   **A virtual environment tool** (like `venv` or `virtualenv`).

## Setup and Installation

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd delivery-poc
    ```

2.  **Create and activate a virtual environment:**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
    ```

3.  **Install the dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

## Configuration

The application requires one environment variable to be set:

*   `POD_BUCKET`: The name of the Google Cloud Storage bucket where proof of delivery files will be stored.

You can set this environment variable in your shell before running the application:
```bash
export POD_BUCKET="your-gcs-bucket-name"
```
On Windows:
```powershell
$env:POD_BUCKET="your-gcs-bucket-name"
```

## Running the Application

To run the application locally, use the following command:

```bash
uvicorn main:app --host 0.0.0.0 --port 8080
```

The application will be available at `http://localhost:8080`.

## Project Structure

Here is a brief overview of the key files in the project:

*   **`main.py`:** This is the main application file containing the FastAPI server, all the API endpoints, and the business logic for managing delivery runs and orders. It also contains the HTML, CSS, and JavaScript for the frontend.
*   **`app.yaml`:** This is a configuration file for deploying the application to a platform like Google App Engine. It specifies the Python runtime and the entrypoint command.
*   **`requirements.txt`:** This file lists the Python dependencies required for the project.
*   **`AI_Onboarding/`:** This directory was present in the project. Its purpose is not clear.
