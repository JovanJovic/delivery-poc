To use the email notification functionality, you will need to:

1.  Create a `.env` file in the root of the project.
2.  Add the following line to the `.env` file:

    ```
    SENDGRID_API_KEY="YOUR_SENDGRID_API_KEY"
    ```

    Replace `"YOUR_SENDGRID_API_KEY"` with your actual SendGrid API key.

3.  Make sure that the `pod_email` is set when creating a run. This is the email address that the proof of delivery will be sent to.